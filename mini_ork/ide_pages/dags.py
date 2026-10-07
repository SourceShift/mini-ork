"""``mini-ork board page dags`` — compact per-run workflow DAGs for many runs.

Designed for the Zed fork's Threads board, which polls this endpoint and
draws one thumbnail per row. ``args["ids"]`` is a CSV of run ids; absent or
empty falls back to the N=50 most recent runs so the page is usable from
the CLI on its own. The page is batched end-to-end — one ``fetch_run_recipes``
round-trip and one ``fetch_node_lifecycle_events_bulk`` round-trip per call,
never one query per run.

Per-run isolation is the contract: one unreadable run lands in
``errors[run_id]`` and must not remove or abort the other thumbnails.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mini_ork.ide_pages import _lanes
from mini_ork.web.db import db_for
from mini_ork.web.repositories import (
    RunDetailRepository,
    derive_node_statuses,
)

# Fallback window when ``args["ids"]`` is absent or empty. The board is
# polled; 50 is enough rows to feel live without dragging a whole-day tail.
# Threaded into ``RunDetailRepository.fetch_run_recipes`` so the SQL
# ``LIMIT`` and the page contract share one constant.
DEFAULT_PAGE_SIZE = 50


def _parse_ids(raw: str | None) -> list[str]:
    """Split a CSV of run ids into a clean list, stripping whitespace.

    ``args["ids"]`` arrives as a free-form CSV from the CLI; without
    stripping, ``"run-a, run-b"`` would yield ``' run-b'`` and the recipe
    lookup below would miss, putting the run into ``errors`` instead of
    ``dags``. ``_lanes.lane_map`` strips its own CSV split; this keeps the
    contract consistent.
    """
    if not raw:
        return []
    return [tok.strip() for tok in raw.split(",") if tok.strip()]


def _workflow_safe(name: str, home: Path) -> dict[str, Any]:
    """Home-aware read of a recipe's workflow, normalised to fingerprint's shape.

    Returns ``{nodes: [{name, lane=model_lane, ...}], edges: [...]}`` so the
    downstream ``_lanes.lane_map`` lookup (role key → provider lane) works
    identically to when ``mini_ork.web.recipes.fingerprint`` was the reader.
    Returns ``{}`` for any failure; the caller records it per run.

    Why not ``wf_recipes.fingerprint(name, home)``? It ultimately calls
    ``load_recipe`` which resolves ``<engine_root>/recipes/<name>/...`` and
    ignores ``home`` — every project-overlay recipe surfaces as
    ``"recipe not found"`` and no thumbnail renders. ``_lanes.recipe_dir``
    honours the same overlay precedence the run page uses.
    """
    wf = _lanes.workflow_yaml(home, name)
    if not wf:
        return {}
    nodes: list[dict[str, Any]] = []
    for n in wf.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        nodes.append({
            "name": n.get("name"),
            "type": n.get("type"),
            # ``lane`` is the recipe's role key (model_lane) — the dags
            # output contract then resolves it to a provider family via
            # ``_lanes.lane_map``. Mirrors fingerprint's per-node shape.
            "lane": n.get("model_lane"),
        })
    return {"nodes": nodes, "edges": wf.get("edges") or []}


def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    """Return ``{ok, label, dags, errors}`` for the Threads board."""
    del tab  # the dags page has no sub-tabs.
    ids = _parse_ids(args.get("ids"))

    db = db_for(home)
    repo = RunDetailRepository(db)

    # fetch_run_recipes(ids=None) → 50 most recent runs (fallback path);
    # fetch_run_recipes(ids=[...]) → just those ids (CLI path). The
    # ``limit=DEFAULT_PAGE_SIZE`` keeps the SQL ``LIMIT`` and the page
    # contract aligned.
    recipes = repo.fetch_run_recipes(ids if ids else None,
                                     limit=DEFAULT_PAGE_SIZE)
    ids_to_show = ids if ids else list(recipes.keys())

    if not ids_to_show:
        # Nothing to render — empty project or fresh state.db.
        return {"ok": True, "label": "DAGs", "dags": {}, "errors": {}}

    # One round-trip for all node lifecycle events across the batch.
    lifecycle = repo.fetch_node_lifecycle_events_bulk(ids_to_show)

    # Distinct recipe names → one workflow read each. ``_lanes.workflow_yaml``
    # routes through ``_cached_yaml`` (lru_cache), so repeats across runs
    # cost zero — the read-once contract matches what ``load_recipe`` used
    # to give us via ``@lru_cache(maxsize=64)`` in ``web/recipes.py``.
    distinct_recipes = {recipes[rid] for rid in ids_to_show if recipes.get(rid)}
    workflows: dict[str, dict[str, Any]] = {
        name: _workflow_safe(name, home) for name in distinct_recipes
    }

    dags: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}

    for run_id in ids_to_show:
        recipe_name = recipes.get(run_id)
        if not recipe_name:
            # The CLI passed an id that is not in ``task_runs`` — an
            # unknown run, not a missing recipe. Skip silently: an empty
            # payload for an unknown id must not surface as a per-run error
            # (see ``test_empty_state_db_returns_an_empty_payload``).
            continue
        wf = workflows.get(recipe_name) or {}
        nodes_raw = wf.get("nodes") or []
        edges_raw = wf.get("edges") or []
        if not nodes_raw:
            # workflow read returned ``{}`` for this recipe (recipe dir gone,
            # yaml parse error, etc.) — surface it; do NOT emit a half-built
            # DAG.
            errors[run_id] = "recipe not found"
            continue

        run_dir = home / "runs" / run_id
        # ``_lanes.lane_map`` caches home/engine agents.yaml internally
        # (lru_cache), so the only per-run read is the run-snapshot itself.
        lane_map = _lanes.lane_map(home, run_dir)
        rows = lifecycle.get(run_id, [])
        statuses = derive_node_statuses(rows)

        nodes: list[dict[str, Any]] = []
        for n in nodes_raw:
            if not isinstance(n, dict):
                continue
            name = n.get("name")
            if not name:
                continue
            role_lane = n.get("lane")  # the recipe's role key (worker, planner…)
            # Provider lane, or the recipe's own key when unmapped. A ``None``
            # role_lane (workflow node without model_lane) stays ``None``,
            # matching fingerprint's own family=None.
            lane = lane_map.get(role_lane, role_lane) if role_lane else None
            entry = statuses.get(str(name), {})
            nodes.append({
                "id": str(name),
                "lane": lane,
                "state": entry.get("status", "never_seen"),
            })

        edges: list[dict[str, Any]] = []
        for e in edges_raw:
            if not isinstance(e, dict):
                continue
            src = e.get("from")
            tgt = e.get("to")
            et = e.get("edge_type")
            if not (src and tgt and et):
                continue
            edges.append({"from": str(src), "to": str(tgt), "edge_type": str(et)})

        dags[run_id] = {
            "recipe": str(recipe_name),
            "nodes": nodes,
            "edges": edges,
        }

    return {"ok": True, "label": "DAGs", "dags": dags, "errors": errors}