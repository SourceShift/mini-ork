"""Dispatch — the topology composer's backend.

The composer is not a DAG editor. It is a bet, priced against evidence mini-ork has already
collected on THIS repo. So this module's job is:

  GET  /options   →  the recipes, the lanes, and WHAT EACH ONE HAS ACTUALLY COST AND WON here
  POST /dispatch  →  record what was proposed, what was chosen, and by whom; mint the run-id

The second endpoint is the important one, and it is not really about dispatching. It is about
capturing the one signal the product was throwing away:

    the system proposed X · a human chose Y · Y turned out (better | worse | the same)

That is a labelled example. Today `conductor_decisions` records only the final choice, so a
human correction is indistinguishable from the machine agreeing with itself. Migration 0050
adds `proposed_*` + `decided_by` + `task_run_id`; this endpoint is what fills them.

HONESTY RULES, enforced here rather than left to the UI:
  · a (lane, task_class) pair with fewer than MIN_SAMPLES runs is returned with
    `evidence: "none"` and NO rate — an advantage from n=2 is noise wearing a number's clothes.
  · every rate ships with a Wilson interval. A bare 75% from n=4 invites belief the sample
    cannot support.
  · we do NOT return the conductor's predicted_score. Until enough decisions carry a
    realized_score, that number is unfalsifiable, and putting it on screen would be exactly the
    kind of confident-but-unaccountable figure this project has already had to retract once.
"""

from __future__ import annotations

import math
import secrets
import time
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..db import StateDB
from ..deps import get_db

router = APIRouter(prefix="/api/v1/dispatch", tags=["dispatch"])

# Below this, a rate is not evidence. It is a coincidence with a percent sign.
MIN_SAMPLES = 5


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - m) / d, (c + m) / d)


def _recipes_root() -> Path:
    # The recipes live in the mini-ork install, not the workspace.
    return Path(__file__).resolve().parents[3] / "recipes"


@router.get("/options")
def options(task_class: str | None = None, db: StateDB = Depends(get_db)) -> dict[str, Any]:
    """Everything the composer needs to price an edit — all of it measured, none of it guessed.

    `task_class` scopes the evidence. A lane's win rate on `code_fix` says nothing about its
    win rate on `research_synthesis`, and averaging across them is how you get a confident lie.
    """
    # ── the recipes on disk (the topologies you can actually run) ────────────
    recipes: list[dict[str, Any]] = []
    root = _recipes_root()
    if root.is_dir():
        for d in sorted(root.iterdir()):
            wf = d / "workflow.yaml"
            if not wf.is_file():
                continue
            try:
                spec = yaml.safe_load(wf.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            if task_class and spec.get("task_class") != task_class:
                continue
            recipes.append(
                {
                    "recipe": d.name,
                    "task_class": spec.get("task_class"),
                    "description": spec.get("description"),
                    "nodes": [
                        {
                            "name": n.get("name"),
                            "type": n.get("type"),
                            "lane": n.get("model_lane"),
                            "dispatch_mode": n.get("dispatch_mode", "serial"),
                            "gates": n.get("gates") or [],
                            "verifier_ref": n.get("verifier_ref"),
                        }
                        for n in (spec.get("nodes") or [])
                    ],
                }
            )

    # ── what each LANE has actually cost and won, here, on this task_class ───
    lanes: list[dict[str, Any]] = []
    if db.has_table("agent_performance_memory"):
        where = "WHERE runs_count > 0"
        params: tuple[Any, ...] = ()
        if task_class:
            where += " AND task_class = ?"
            params = (task_class,)
        for r in db.rows(
            f"""SELECT agent_version_id, task_class, runs_count, success_count,
                       avg_cost_usd, avg_duration_ms, relative_advantage, top_failure_modes
                FROM agent_performance_memory {where}
                ORDER BY runs_count DESC""",  # noqa: S608 — `where` is built from fixed fragments
            params,
        ):
            n = int(r["runs_count"] or 0)
            k = int(r["success_count"] or 0)
            thin = n < MIN_SAMPLES
            lo, hi = _wilson(k, n)
            lanes.append(
                {
                    "lane": r["agent_version_id"],
                    "task_class": r["task_class"],
                    "runs": n,
                    "successes": k,
                    "avg_cost_usd": r["avg_cost_usd"],
                    "avg_duration_ms": r["avg_duration_ms"],
                    "advantage": r["relative_advantage"],
                    "top_failure_modes": r["top_failure_modes"],
                    # A thin sample gets NO rate. Not a dimmed one — none. If the UI can read
                    # a number it will render a number, and a rendered number gets believed.
                    "success_rate": None if thin else k / n,
                    "ci": None if thin else [lo, hi],
                    "evidence": "none" if thin else "measured",
                }
            )

    # ── what each TOPOLOGY has won, and what it charged for it ──────────────
    topologies: list[dict[str, Any]] = []
    if db.has_table("topology_win_rates"):
        where = "WHERE sample_size > 0"
        params = ()
        if task_class:
            where += " AND task_class = ?"
            params = (task_class,)
        for r in db.rows(
            f"""SELECT topology_id, workflow_name, task_class, wins, losses, ties,
                       win_rate, sample_size, avg_cost_usd, avg_duration_ms
                FROM topology_win_rates {where}
                ORDER BY sample_size DESC""",  # noqa: S608
            params,
        ):
            n = int(r["sample_size"] or 0)
            k = int(r["wins"] or 0)
            thin = n < MIN_SAMPLES
            lo, hi = _wilson(k, n)
            topologies.append(
                {
                    "topology_id": r["topology_id"],
                    "workflow_name": r["workflow_name"],
                    "task_class": r["task_class"],
                    "wins": k,
                    "losses": r["losses"],
                    "sample_size": n,
                    "win_rate": None if thin else float(r["win_rate"] or 0),
                    "ci": None if thin else [lo, hi],
                    "avg_cost_usd": r["avg_cost_usd"],
                    "avg_duration_ms": r["avg_duration_ms"],
                    "evidence": "none" if thin else "measured",
                }
            )

    return {
        "recipes": recipes,
        "lanes": lanes,
        "topologies": topologies,
        "min_samples": MIN_SAMPLES,
        # Deliberately absent: the conductor's predicted_score. See the module docstring.
        "note": (
            "Rates are measured on this repo. A lane or topology with fewer than "
            f"{MIN_SAMPLES} runs returns evidence='none' and no rate — not a dimmed one."
        ),
    }


class DispatchRequest(BaseModel):
    """What the composer sends when the user hits Dispatch.

    NOT the provider-transport ``DispatchRequest`` dataclass in
    ``mini_ork/dispatch/models.py`` — same name, different layer: this one is
    the web API contract, that one is the LLM dispatch primitive."""

    kickoff: str
    recipe: str
    task_class: str | None = None
    worktree: str | None = None

    # What the machine suggested, before the human touched it. This is half of the
    # labelled example; without it an override is invisible.
    proposed_recipe: str | None = None
    proposed_topology: str | None = None
    proposed_lane_hints: str | None = None

    # What the human actually chose. `lane_overrides` rides into the run as env.
    lane_overrides: dict[str, str] = Field(default_factory=dict)
    override_reason: str | None = None

    epic_id: str | None = None


class DispatchResponse(BaseModel):
    run_id: str
    command: str
    env: dict[str, str]
    decision_id: int | None
    decided_by: str
    overrode: bool


@router.post("", response_model=DispatchResponse)
def dispatch(
    req: DispatchRequest,
    db: StateDB = Depends(get_db),
) -> DispatchResponse:
    """Mint the run-id, record the decision, and hand back the exact command to spawn.

    mini-ork does NOT spawn the process. The caller (Orca) does, via its own PTY — which is
    what gives you a native, attachable, persistent terminal for free. We only decide the id,
    so that the caller can subscribe to the trajectory BEFORE the process starts. That is what
    makes the correlation deterministic instead of a race (see orca/coevolve/ATTACH.md).

    `MINI_ORK_RUN_ID` is injectable; the Python CLI keeps a supplied value and
    generates one only when the caller did not provide it.
    """
    if not req.kickoff.strip():
        raise HTTPException(status_code=400, detail="kickoff is required")

    # A COLLISION HERE CORRUPTS A RUN. Two dispatches of the same kickoff inside one second
    # must not share a run dir. An earlier version keyed on `hash(kickoff) % 100000`, which
    # collided on the very first test — and Python's hash() is not even stable across
    # processes (PYTHONHASHSEED), so the id was neither unique nor reproducible.
    run_id = f"run-{int(time.time())}-{secrets.token_hex(4)}"

    chosen_hints = ",".join(f"{node}={lane}" for node, lane in sorted(req.lane_overrides.items()))

    # An override is a DIFFERENCE, not a flag someone remembered to set. Derive it, so the UI
    # cannot lie about it — deliberately or by omission.
    overrode = bool(
        (req.proposed_recipe and req.proposed_recipe != req.recipe)
        or (req.proposed_lane_hints or "") != chosen_hints
    )
    decided_by = "human" if overrode else "conductor"

    # StateDB is deliberately READ-ONLY (the web layer is documented as a read-only surface
    # over state.db). Recording a decision is the one legitimate write, so it takes its own
    # short-lived connection rather than punching an execute() hole in the read layer that
    # every future route would reach for.
    decision_id: int | None = None
    if db.has_table("conductor_decisions"):
        cols = {r["name"] for r in db.rows("PRAGMA table_info(conductor_decisions)")}
        if "task_run_id" in cols:  # migration 0050 applied
            con = sqlite3.connect(db.db_path, timeout=5.0)
            con.execute("PRAGMA busy_timeout=5000")
            try:
                cur = con.execute(
                    """INSERT INTO conductor_decisions
                         (decided_at, epic_id, task_class, task_run_id,
                          proposed_recipe, proposed_topology, proposed_lane_hints,
                          chosen_recipe, chosen_topology, chosen_lane_hints,
                          decided_by, override_reason, outcome)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'pending')""",
                    (
                        int(time.time()),
                        req.epic_id,
                        req.task_class,
                        run_id,
                        req.proposed_recipe,
                        req.proposed_topology,
                        req.proposed_lane_hints,
                        req.recipe,
                        req.proposed_topology,
                        chosen_hints or None,
                        decided_by,
                        req.override_reason,
                    ),
                )
                con.commit()
                decision_id = cur.lastrowid
            finally:
                con.close()

    env = {"MINI_ORK_RUN_ID": run_id}
    for node, lane in req.lane_overrides.items():
        # Per-node lane override. `llm_dispatch --model <lane>` is the real flag; the runner
        # reads these per-node vars. (MINI_ORK_LANE_OVERRIDE does not exist — do not invent it.)
        env[f"MINI_ORK_LANE_{node.upper()}"] = lane

    return DispatchResponse(
        run_id=run_id,
        command=f"bin/mini-ork run {req.recipe} {req.kickoff}",
        env=env,
        decision_id=decision_id,
        decided_by=decided_by,
        overrode=overrode,
    )
