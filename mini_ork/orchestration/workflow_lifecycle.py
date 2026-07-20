"""Workflow memory/candidate persistence — Python port of lib/workflow_lifecycle.sh.

ensure_baseline: one 'promoted' workflow_memory row per recipe (yaml_blob from
recipes/<recipe>/workflow.yaml), idempotent, returns workflow_version_id.
candidate_store: persist a group_propose candidate into workflow_candidates
(auto-creating the FK baseline), returns candidate_id. Faithful to the bash,
including the task_class->recipe-dir underscore/hyphen convention.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid


def _db_path(db: str | None) -> str:
    if db:
        return db
    env = os.environ.get("MINI_ORK_DB")
    if not env:
        raise RuntimeError("MINI_ORK_DB unset")
    return env


def _root(root: str | None) -> str:
    return root or os.environ.get("MINI_ORK_ROOT", ".")


def ensure_baseline(recipe: str, task_class: str | None = None,
                    db: str | None = None, root: str | None = None) -> str:
    """Idempotently create the baseline workflow_memory row; return version_id.
    Raises FileNotFoundError when the recipe has no workflow.yaml (bash exits 1)."""
    root = _root(root)
    yaml_path = os.path.join(root, "recipes", recipe, "workflow.yaml")
    if not os.path.isfile(yaml_path):
        raise FileNotFoundError(f"no workflow.yaml at {yaml_path}")
    yaml_blob = open(yaml_path, encoding="utf-8").read()
    yaml_hash = hashlib.sha256(yaml_blob.encode("utf-8")).hexdigest()
    version_id = f"{recipe}_v0.1.0"

    con = sqlite3.connect(_db_path(db))
    con.execute("PRAGMA busy_timeout=5000")
    try:
        if con.execute("SELECT workflow_version_id FROM workflow_memory "
                       "WHERE workflow_version_id = ?", (version_id,)).fetchone():
            return version_id
        con.execute(
            "INSERT INTO workflow_memory (workflow_version_id, workflow_name, "
            "base_version_id, yaml_hash, yaml_blob, mutations, status) "
            "VALUES (?, ?, NULL, ?, ?, '[]', 'promoted')",
            (version_id, recipe, yaml_hash, yaml_blob))
        con.commit()
        return version_id
    finally:
        con.close()


def candidate_store(payload: dict | str, db: str | None = None,
                    root: str | None = None) -> str:
    """Persist a group_propose candidate; auto-creates the baseline FK row.
    Returns candidate_id. Raises ValueError/FileNotFoundError on bad input
    (bash writes stderr + exits 1)."""
    root = _root(root)
    p = json.loads(payload) if isinstance(payload, str) else payload
    if not isinstance(p, dict):
        raise ValueError(f"expected object, got {type(p).__name__}")

    task_class = p.get("task_class") or "generic"
    recipe = task_class.replace("_", "-")
    recipe_dir = os.path.join(root, "recipes", recipe)
    if not os.path.isdir(recipe_dir):
        recipe = task_class
        recipe_dir = os.path.join(root, "recipes", recipe)
    if not os.path.isdir(recipe_dir):
        raise FileNotFoundError(f"no recipes/ dir for task_class={task_class}")

    yaml_path = os.path.join(recipe_dir, "workflow.yaml")
    if not os.path.isfile(yaml_path):
        raise FileNotFoundError(f"no workflow.yaml at {yaml_path}")
    yaml_blob = open(yaml_path, encoding="utf-8").read()
    yaml_hash = hashlib.sha256(yaml_blob.encode("utf-8")).hexdigest()
    version_id = f"{recipe}_v0.1.0"

    candidate_id = p.get("candidate_id") or f"wc-{uuid.uuid4().hex[:12]}"
    mutations = (json.dumps([p.get("mutation_applied", {})])
                 if p.get("mutation_applied") else "[]")

    con = sqlite3.connect(_db_path(db))
    con.execute("PRAGMA busy_timeout=5000")
    try:
        if not con.execute("SELECT workflow_version_id FROM workflow_memory "
                           "WHERE workflow_version_id = ?", (version_id,)).fetchone():
            con.execute(
                "INSERT INTO workflow_memory (workflow_version_id, workflow_name, "
                "base_version_id, yaml_hash, yaml_blob, mutations, status) "
                "VALUES (?, ?, NULL, ?, ?, '[]', 'promoted')",
                (version_id, recipe, yaml_hash, yaml_blob))
        con.execute(
            "INSERT INTO workflow_candidates (candidate_id, base_workflow_version_id, "
            "mutations, status, utility_delta, created_by) "
            "VALUES (?, ?, ?, 'candidate', 0.0, 'evolution_engine') "
            "ON CONFLICT(candidate_id) DO NOTHING",
            (candidate_id, version_id, mutations))
        con.commit()
        return candidate_id
    finally:
        con.close()
