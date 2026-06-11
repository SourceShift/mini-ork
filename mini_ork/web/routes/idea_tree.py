"""Idea Tree HTTP routes.

Per docs/plans/2026-06-11-arbor-techniques-into-mini-ork.md item 1.

Endpoints (all read-only, loopback by default):
  GET /api/v1/idea-tree/roots            — list every root node
  GET /api/v1/idea-tree/{root_node_id}   — full subtree under one root
  GET /api/v1/idea-tree/node/{node_id}   — single node + parent/children refs
  GET /api/v1/idea-tree/node/{node_id}/ancestors — leaf-to-root chain

Why "node/" prefix on the single-node endpoints: avoids ambiguity with the
subtree route. Without it, GET /api/v1/idea-tree/itn-self-improve-xyz would
race the more permissive {root_node_id} matcher.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam

from .. import idea_tree
from ..db import StateDB
from ..deps import get_db

router = APIRouter(prefix="/api/v1/idea-tree", tags=["idea-tree"])


@router.get("/roots")
def list_roots(db: StateDB = Depends(get_db)) -> list[dict[str, Any]]:
    """Every root node, newest first. Includes subtree counts per root."""
    return idea_tree.list_roots(db)


@router.get("/node/{node_id}")
def get_node(
    node_id: str = PathParam(..., description="idea_tree_nodes.node_id"),
    db: StateDB = Depends(get_db),
) -> dict[str, Any]:
    """One node + parent/children refs."""
    out = idea_tree.read_node(db, node_id)
    if not out:
        raise HTTPException(status_code=404, detail=f"node {node_id} not found")
    return out


@router.get("/node/{node_id}/ancestors")
def get_ancestors(
    node_id: str = PathParam(...),
    db: StateDB = Depends(get_db),
) -> list[dict[str, Any]]:
    """Leaf-to-root chain. Empty list if node_id doesn't exist."""
    return idea_tree.walk_to_root(db, node_id)


@router.get("/{root_node_id}")
def get_tree(
    root_node_id: str = PathParam(..., description="idea_tree_nodes.node_id (must be a root)"),
    db: StateDB = Depends(get_db),
) -> dict[str, Any]:
    """Full subtree rooted at root_node_id.

    Works for any node — passing a non-root id returns the subtree under
    that node. The route name says "root" because the typical UI flow is
    list_roots → pick one → fetch its tree.
    """
    tree = idea_tree.read_tree(db, root_node_id)
    if not tree["nodes"]:
        raise HTTPException(status_code=404, detail=f"no nodes under {root_node_id}")
    return tree
