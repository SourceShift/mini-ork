#!/usr/bin/env python3
"""Per-wave fan-out body for the registry-audit recipe's ``audit_items`` node.

Registered as the ``(registry-audit, audit_items)`` implementer submode, so the
dispatch layer runs this file with no args. It reads
``<run_dir>/audit-plan.json``, runs the shared checkpointed pool from
``mini_ork.orchestration.item_fanout`` over the planned items, and writes
``<run_dir>/audit-result.json`` (the node's ``results_artifact``).

Why a driver rather than a workflow-level fan-out: the work list is data (the
registry's rows), and the DAG cannot know its width at compile time. Four
recipes re-implemented this loop; the shared module is where the bounded pool
and the per-item checkpoint now live.

Env:
  MO_REGISTRY_MAX_PARALLEL   worker pool size (default 4; 1 = sequential)
  MO_REGISTRY_BUDGET_USD     stop admitting items past this spend (default off)
  MO_REGISTRY_ITEM_TIMEOUT_S per-item deadline in seconds (default off)
  MO_REGISTRY_CHILD_RECIPE   recipe the audit child runs (required to spawn)
  MO_REGISTRY_CHILD_KICKOFF  kickoff file or inline body for the child
  MO_REGISTRY_DRY=1          record the plan without spawning (test seam)
  MO_REGISTRY_NO_EXECUTE=1   spawn children but do not execute them
  MINI_ORK_RUN_ID            parent run id stamped on each child
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# Direct ``python3 recipes/registry-audit/lib/fanout_driver.py`` runs from any
# cwd; the dispatch path already has mini_ork importable. Add the repo root only
# if the import would otherwise fail, so we never shadow an installed package.
try:  # pragma: no cover - environment probe
    import mini_ork  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    _ROOT = Path(__file__).resolve().parents[3]
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))

from mini_ork.orchestration.item_fanout import STATUS_OK, run_items  # noqa: E402


def _run_dir() -> Path:
    run_dir = os.environ.get("MINI_ORK_RUN_DIR")
    if not run_dir:
        raise SystemExit("fanout_driver: MINI_ORK_RUN_DIR is not set")
    return Path(run_dir)


def _float_env(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _optional_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _render_kickoff(item: dict[str, Any], registry_path: str) -> str:
    """Materialize the child kickoff for one item.

    The template is read from ``MO_REGISTRY_CHILD_KICKOFF`` (a file path or an
    inline body). Placeholders are substituted per item; a template with no
    placeholders renders identically for every item and is not a bug.
    """
    template = os.environ.get("MO_REGISTRY_CHILD_KICKOFF", "")
    if not template:
        raise ValueError("MO_REGISTRY_CHILD_KICKOFF is required to spawn an audit child")
    body = Path(template).read_text(encoding="utf-8") if Path(template).is_file() else template
    rendered = (
        body.replace("{{item_id}}", str(item.get("id", "")))
        .replace("{{title}}", str(item.get("title", "")))
        .replace("{{status}}", str(item.get("status", "")))
        .replace("{{cluster}}", str(item.get("cluster", "")))
        .replace("{{registry_path}}", registry_path)
    )
    return rendered


def _make_worker(registry_path: str):
    """Build the per-item worker. Kept as a factory so the dry-run path never
    imports the spawn machinery."""

    def worker(item: dict[str, Any]) -> dict[str, Any]:
        if os.environ.get("MO_REGISTRY_DRY", "").strip() == "1":
            return {"status": STATUS_OK, "item_id": item.get("id"), "dry_run": True}

        child_recipe = os.environ.get("MO_REGISTRY_CHILD_RECIPE", "").strip()
        if not child_recipe:
            raise ValueError("MO_REGISTRY_CHILD_RECIPE is required to spawn an audit child")

        from mini_ork.cli.spawn import spawn  # lazy: keeps the dry path import-light

        run_dir = _run_dir()
        kickoff_text = _render_kickoff(item, registry_path)
        # Write the per-item kickoff into the run dir rather than a tmp path: a
        # failed child's exact prompt is then re-readable from the run receipt.
        slug = str(item.get("id", "item")).replace("/", "_")
        kickoff_path = run_dir / f"_audit_kickoff_{slug}.md"
        kickoff_path.write_text(kickoff_text, encoding="utf-8")

        result = spawn(
            parent_run=os.environ.get("MINI_ORK_RUN_ID", ""),
            kickoff=str(kickoff_path),
            recipe=child_recipe,
            allow_child_spawn=int(os.environ.get("MINI_ORK_ALLOW_CHILD_SPAWN", "0")),
            no_execute=int(os.environ.get("MO_REGISTRY_NO_EXECUTE", "0")),
        )
        return {
            "status": STATUS_OK if result.exit_code == 0 else "failed",
            "item_id": item.get("id"),
            "child_recipe": child_recipe,
            "spawn_id": result.spawn_id,
            "child_run_id": result.child_run_id or None,
            "exit_code": result.exit_code,
        }

    return worker


def run_fanout(run_dir: Path | None = None) -> dict[str, Any]:
    """Run the pool over ``audit-plan.json`` and write ``audit-result.json``."""
    run_dir = run_dir or _run_dir()
    plan_path = run_dir / "audit-plan.json"
    if not plan_path.is_file():
        raise SystemExit(f"fanout_driver: plan not found at {plan_path}")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    pending = plan.get("pending", [])
    registry_path = plan.get("registry_path", "")

    result = run_items(
        pending,
        results_dir=run_dir / "results",
        worker=_make_worker(registry_path),
        max_workers=_optional_int("MO_REGISTRY_MAX_PARALLEL") or 4,
        per_item_timeout=_float_env("MO_REGISTRY_ITEM_TIMEOUT_S"),
        budget_usd=_float_env("MO_REGISTRY_BUDGET_USD"),
    )

    payload = result.to_dict()
    payload["registry_path"] = registry_path
    payload["total_items"] = plan.get("total_items", 0)
    (run_dir / "audit-result.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return payload


def main() -> int:
    payload = run_fanout()
    # The executor grades a node on stdout; an empty capture reads as a vacuous
    # result, so always print the manifest summary.
    print(json.dumps({"verdict": payload.get("verdict"), "planned": payload.get("total"),
                      "ok": payload.get("ok"), "deferred": payload.get("deferred")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
