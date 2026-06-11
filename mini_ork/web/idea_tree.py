"""Idea Tree read-side helpers.

Per docs/plans/2026-06-11-arbor-techniques-into-mini-ork.md item 1, the
idea_tree_nodes table is a generic tree primitive borrowed from Arbor's
hypothesis-tree architecture. This module exposes:

  list_roots(db)             — every root (node with no parent)
  read_tree(db, root_id)     — full subtree under a root, flattened with depth
  read_node(db, node_id)     — one node with its parent + children pointers
  walk_to_root(db, node_id)  — ancestor chain from any node up to its root

All reads are PRAGMA query_only-safe — no writes.

Schema reference: db/migrations/0020_idea_tree.sql
"""

from __future__ import annotations

import json
from typing import Any

from .db import StateDB


# ── Roots ────────────────────────────────────────────────────────────────


def list_roots(db: StateDB) -> list[dict[str, Any]]:
    """Every root node (parent_node_id IS NULL), newest first.

    Each row includes a node_count for the whole subtree so the UI can
    render "Self-improve session on 2026-06-09 — 52 iters" without a
    second query.
    """
    if not db.has_table("idea_tree_nodes"):
        return []
    return db.rows(
        """
        SELECT n.node_id,
               n.recipe,
               n.hypothesis,
               n.status,
               n.created_at,
               n.updated_at,
               (SELECT COUNT(*) FROM idea_tree_nodes c
                  WHERE c.root_node_id = n.node_id) AS node_count,
               (SELECT COUNT(*) FROM idea_tree_nodes c
                  WHERE c.root_node_id = n.node_id
                    AND c.status = 'harvested') AS harvested_count,
               (SELECT COUNT(*) FROM idea_tree_nodes c
                  WHERE c.root_node_id = n.node_id
                    AND c.status = 'pruned') AS pruned_count
        FROM idea_tree_nodes n
        WHERE n.parent_node_id IS NULL
        ORDER BY n.created_at DESC
        """
    )


# ── Subtree ─────────────────────────────────────────────────────────────


def read_tree(db: StateDB, root_node_id: str) -> dict[str, Any]:
    """Return the full subtree rooted at root_node_id.

    Shape:
      {
        "root_node_id": "...",
        "nodes": [
          { node_id, parent_node_id, recipe, hypothesis, status,
            score_dev, score_test, insights, depth,
            task_run_id, self_improve_run_id,
            created_at, updated_at },
          ...
        ],
        "edges": [ { "from": parent_id, "to": child_id }, ... ],
        "stats": { total, by_status: {...}, max_depth }
      }

    Depth is computed via recursive CTE so the UI can render the tree
    without a second walk.
    """
    if not db.has_table("idea_tree_nodes"):
        return {"root_node_id": root_node_id, "nodes": [], "edges": [], "stats": {}}

    # Recursive CTE: walk down from root_node_id, tagging each node with depth.
    # Anchor row uses parent_node_id IS NULL because the root, by definition,
    # has no parent. Recursive arm joins children by parent_node_id.
    rows = db.rows(
        """
        WITH RECURSIVE subtree(node_id, parent_node_id, depth) AS (
            SELECT n.node_id, n.parent_node_id, 0
            FROM idea_tree_nodes n
            WHERE n.node_id = ?
          UNION ALL
            SELECT c.node_id, c.parent_node_id, s.depth + 1
            FROM idea_tree_nodes c
            JOIN subtree s ON c.parent_node_id = s.node_id
        )
        SELECT n.node_id,
               n.parent_node_id,
               n.recipe,
               n.task_run_id,
               n.self_improve_run_id,
               n.hypothesis,
               n.status,
               n.score_dev,
               n.score_test,
               n.insights_json,
               n.created_at,
               n.updated_at,
               s.depth
        FROM subtree s
        JOIN idea_tree_nodes n ON n.node_id = s.node_id
        ORDER BY s.depth ASC, n.created_at ASC
        """,
        (root_node_id,),
    )

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    by_status: dict[str, int] = {}
    max_depth = 0

    for r in rows:
        try:
            insights = json.loads(r.get("insights_json") or "[]")
        except json.JSONDecodeError:
            insights = []
        nodes.append(
            {
                "node_id": r["node_id"],
                "parent_node_id": r.get("parent_node_id"),
                "recipe": r.get("recipe"),
                "task_run_id": r.get("task_run_id"),
                "self_improve_run_id": r.get("self_improve_run_id"),
                "hypothesis": r.get("hypothesis"),
                "status": r["status"],
                "score_dev": r.get("score_dev"),
                "score_test": r.get("score_test"),
                "insights": insights,
                "depth": r["depth"],
                "created_at": r.get("created_at"),
                "updated_at": r.get("updated_at"),
            }
        )
        if r.get("parent_node_id"):
            edges.append({"from": r["parent_node_id"], "to": r["node_id"]})
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        max_depth = max(max_depth, r["depth"])

    return {
        "root_node_id": root_node_id,
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "total": len(nodes),
            "by_status": by_status,
            "max_depth": max_depth,
        },
    }


# ── Single node ─────────────────────────────────────────────────────────


def read_node(db: StateDB, node_id: str) -> dict[str, Any] | None:
    """One node row plus references to parent and immediate children."""
    if not db.has_table("idea_tree_nodes"):
        return None
    row = db.row(
        """
        SELECT node_id, parent_node_id, root_node_id, recipe,
               task_run_id, self_improve_run_id, hypothesis, status,
               score_dev, score_test, insights_json,
               created_at, updated_at
        FROM idea_tree_nodes WHERE node_id = ?
        """,
        (node_id,),
    )
    if not row:
        return None
    try:
        row["insights"] = json.loads(row.pop("insights_json") or "[]")
    except json.JSONDecodeError:
        row["insights"] = []
        row.pop("insights_json", None)
    row["children"] = db.rows(
        """
        SELECT node_id, hypothesis, status, score_dev, score_test
        FROM idea_tree_nodes WHERE parent_node_id = ?
        ORDER BY created_at ASC
        """,
        (node_id,),
    )
    return row


# ── Ancestor chain ──────────────────────────────────────────────────────


def walk_to_root(db: StateDB, node_id: str) -> list[dict[str, Any]]:
    """Return ancestors from leaf to root, inclusive of both endpoints.

    Used by insight backpropagation (plan item 3) and by the UI to render
    breadcrumb trails from any node back to its root.
    """
    if not db.has_table("idea_tree_nodes"):
        return []
    return db.rows(
        """
        WITH RECURSIVE chain(node_id, parent_node_id, hypothesis, status, depth) AS (
            SELECT n.node_id, n.parent_node_id, n.hypothesis, n.status, 0
            FROM idea_tree_nodes n WHERE n.node_id = ?
          UNION ALL
            SELECT p.node_id, p.parent_node_id, p.hypothesis, p.status, c.depth + 1
            FROM idea_tree_nodes p
            JOIN chain c ON c.parent_node_id = p.node_id
        )
        SELECT node_id, parent_node_id, hypothesis, status, depth
        FROM chain
        ORDER BY depth ASC
        """,
        (node_id,),
    )
