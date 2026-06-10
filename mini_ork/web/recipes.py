"""Load recipe DAG + lane→family resolution for the detection-fingerprint view.

Reads recipes/<name>/workflow.yaml + config/agents.yaml off MINI_ORK_ROOT so
the UI can answer "which model family ran which lens?" without re-running.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:  # PyYAML is an optional dep; degrade gracefully
    yaml = None  # type: ignore[assignment]


def mini_ork_root() -> Path:
    env = os.environ.get("MINI_ORK_ROOT")
    if env:
        return Path(env).resolve()
    # mini_ork/web/recipes.py → repo root is parents[2]
    return Path(__file__).resolve().parents[2]


def _safe_load(path: Path) -> dict[str, Any]:
    if not path.exists() or yaml is None:
        return {}
    try:
        with path.open() as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


@lru_cache(maxsize=64)
def load_recipe(name: str) -> dict[str, Any]:
    root = mini_ork_root()
    wf = _safe_load(root / "recipes" / name / "workflow.yaml")
    tc = _safe_load(root / "recipes" / name / "task_class.yaml")
    return {"name": name, "workflow": wf, "task_class": tc}


@lru_cache(maxsize=16)
def load_lanes(home: Path | None = None) -> dict[str, str]:
    """lane_name → family (e.g. opus_lens → opus, planner → deepseek).

    Mirrors lib/llm-dispatch.sh resolution: the workspace's own
    $MINI_ORK_HOME/config/agents.yaml wins wholesale when present
    (per-project lane overrides), else the repo's config/agents.yaml.
    """
    cfg_path = mini_ork_root() / "config" / "agents.yaml"
    if home is not None:
        override = Path(home) / "config" / "agents.yaml"
        if override.exists():
            cfg_path = override
    cfg = _safe_load(cfg_path)
    lanes = (cfg.get("lanes") or {}) if isinstance(cfg, dict) else {}
    return {str(k): str(v) for k, v in lanes.items()}


def list_recipes() -> list[str]:
    root = mini_ork_root() / "recipes"
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "workflow.yaml").exists())


def fingerprint(recipe_name: str, home: Path | None = None) -> dict[str, Any]:
    """Per-node family attribution + coalition risk score for a recipe.

    Returns:
      {recipe, nodes: [{name, type, lane, family}], families_used,
       coalition: 'heterogeneous'|'low'|'medium'|'high', dominant_family}
    """
    rec = load_recipe(recipe_name)
    wf = rec["workflow"] or {}
    lanes = load_lanes(home)
    nodes_raw = wf.get("nodes") or []
    nodes: list[dict[str, Any]] = []
    for n in nodes_raw:
        if not isinstance(n, dict):
            continue
        lane = n.get("model_lane")
        family = lanes.get(lane, lane) if lane else None
        nodes.append(
            {
                "name": n.get("name"),
                "type": n.get("type"),
                "lane": lane,
                "family": family,
                "prompt_ref": n.get("prompt_ref"),
                "verifier_ref": n.get("verifier_ref"),
                "dispatch_mode": n.get("dispatch_mode"),
                "gates": n.get("gates") or [],
            }
        )
    family_counts: dict[str, int] = {}
    for n in nodes:
        f = n.get("family")
        if f:
            family_counts[f] = family_counts.get(f, 0) + 1
    total = sum(family_counts.values()) or 1
    dominant = max(family_counts, key=lambda k: family_counts[k]) if family_counts else None
    dominant_share = (family_counts.get(dominant, 0) / total) if dominant else 0.0
    if len(family_counts) >= 4:
        coalition = "heterogeneous"
    elif dominant_share >= 0.75:
        coalition = "high"
    elif dominant_share >= 0.5:
        coalition = "medium"
    else:
        coalition = "low"
    return {
        "recipe": recipe_name,
        "nodes": nodes,
        "edges": wf.get("edges") or [],
        "families_used": family_counts,
        "dominant_family": dominant,
        "dominant_share": dominant_share,
        "coalition": coalition,
    }
