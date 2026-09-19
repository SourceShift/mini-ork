"""Deterministic transforms for the goal-loop recipe.

`goal_state_eval` runs the units lister + predicate command once per wave
inside the target repo and materializes the per-unit pass/fail map. The
predicate is invoked as argv (NOT shell) so a unit id cannot escape its
argument position.

`sweep_dispatch_plan` reads the goal-state map, picks up to
MO_GOAL_MAX_CHILDREN_PER_WAVE failing units, and writes the wave's
fix-children plan. U4a only PLANS — actual child spawning arrives with the
U4b driver.

`goal_apply_deploy` CLOSES the loop: after the sweep node produces a fix in
the target worktree, this node deploys it (a caller-supplied commit+push
command), waits for the deploy to land, re-dispatches every swept unit, and
BLOCKS until each unit reaches a terminal state — so the downstream
goal_check verifier observes the true post-deploy result inside the same
wave. It is a no-op passthrough unless armed with MO_GOAL_APPLY=1.

All transforms are decorated with ``@register_transform`` so workflow.yaml
can name them in the ``transform:`` field of type:transform nodes. They run
inside the MiniOrk Python process (NOT inside a coding harness), keeping the
subprocess I/O reproducible and inspectable from the receipt layer.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from mini_ork.workflow.artifacts import ArtifactContractError, ArtifactLedger
from mini_ork.workflow.compiler import CompiledWorkflow
from mini_ork.workflow.transforms import register_transform

# Load the goal_state helpers by file path because ``recipes/`` is not a
# Python package. The recipe-local loader uses
# ``importlib.util.spec_from_file_location`` which gives this module a
# synthetic name without a parent package, so a normal
# ``from .goal_state import ...`` would fail with "attempted relative import
# with no known parent package". Mirror the recipe_register loader pattern
# at ``mini_ork/cli/recipe_register.py:67``.
_GOAL_STATE_PATH = Path(__file__).resolve().parent / "goal_state.py"
_goal_state_spec = importlib.util.spec_from_file_location(
    "goal_loop_goal_state", _GOAL_STATE_PATH,
)
if _goal_state_spec is None or _goal_state_spec.loader is None:
    raise ImportError(f"could not load goal_state helper from {_GOAL_STATE_PATH}")
_goal_state_module = importlib.util.module_from_spec(_goal_state_spec)
sys.modules.setdefault(_goal_state_spec.name, _goal_state_module)
_goal_state_spec.loader.exec_module(_goal_state_module)
evaluate_units = _goal_state_module.evaluate_units
list_units = _goal_state_module.list_units
harvest_evidence = _goal_state_module.harvest_evidence


def _slug(unit_id: str) -> str:
    """Filesystem-safe rendering of a unit id (unit ids are often file paths)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", unit_id)[:80] or "x"


@register_transform("goal_state_eval")
def goal_state_eval(workflow: CompiledWorkflow, ledger: ArtifactLedger, node_id: str) -> Path:
    """Materialize ``goal-state.json`` from MO_GOAL_* env config.

    Inputs:
        ``MO_GOAL_TARGET_CWD`` — absolute path to the target repo.
        ``MO_GOAL_UNITS_CMD``  — command run inside target cwd, one unit id per line.
        ``MO_GOAL_PREDICATE_CMD`` — argv-prefix; ``<unit_id>`` appended per call.

    Output:
        ``<run_dir>/goal-state.json`` mapping ``unit_id -> {"pass": bool, "reason": str}``.
    """
    target_cwd = os.environ.get("MO_GOAL_TARGET_CWD")
    units_cmd = os.environ.get("MO_GOAL_UNITS_CMD")
    predicate_cmd = os.environ.get("MO_GOAL_PREDICATE_CMD")
    if not target_cwd or not units_cmd or not predicate_cmd:
        raise ArtifactContractError(
            "goal_state_eval requires MO_GOAL_TARGET_CWD, MO_GOAL_UNITS_CMD, "
            "and MO_GOAL_PREDICATE_CMD env vars",
        )

    units = list_units(target_cwd, units_cmd)
    states = evaluate_units(target_cwd, predicate_cmd, units)
    node = workflow.nodes[node_id]
    if "goal_state" not in node.outputs:
        raise ArtifactContractError("goal_state_eval requires a goal_state output")
    out_path = ledger.output_path(workflow, node_id, "goal_state")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(states, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out_path


@register_transform("goal_sweep_plan")
def goal_sweep_plan(workflow: CompiledWorkflow, ledger: ArtifactLedger, node_id: str) -> Path:
    """Select failing units for the wave's fix children.

    Reads the upstream ``goal_state`` artifact written by ``goal_state_eval``,
    picks the failing units in deterministic order (sorted by unit_id), and
    caps the selection at ``MO_GOAL_MAX_CHILDREN_PER_WAVE`` (default 3).
    The output ``sweep-plan.json`` lists ``{unit_id, child_recipe, kickoff_hint}``
    entries — the outer U4b driver consumes this and dispatches the fix
    children. U4a only materializes the plan.
    """
    try:
        max_children = int(os.environ.get("MO_GOAL_MAX_CHILDREN_PER_WAVE", "3"))
    except ValueError:
        max_children = 3
    if max_children < 0:
        max_children = 0
    child_recipe = os.environ.get("MO_GOAL_CHILD_RECIPE", "")

    prepared = ledger.prepared_inputs(node_id)
    goal_state_paths = prepared.paths.get("goal_state", ())
    if not goal_state_paths:
        raise ArtifactContractError("goal_sweep_plan requires goal_state input")
    goal_state = json.loads(goal_state_paths[0].read_text(encoding="utf-8"))

    failing = sorted(
        unit_id for unit_id, state in goal_state.items() if not state.get("pass", False)
    )
    selected = failing[:max_children]

    # Deep-evidence harvest (optional): for ONLY the units we're about to
    # dispatch, run MO_GOAL_EVIDENCE_CMD to gather the rich failure signal the
    # one-line predicate reason can't carry. The full text is persisted to
    # <run_dir>/evidence/<slug>.md (so the fix child can re-read it whole) and a
    # copy is threaded into the plan so the spawn templater can inline it as
    # {{evidence}}. Unset MO_GOAL_EVIDENCE_CMD → the child falls back to reason.
    evidence_map = _harvest_selected_evidence(selected, goal_state)

    node = workflow.nodes[node_id]
    if "sweep_plan" not in node.outputs:
        raise ArtifactContractError("goal_sweep_plan requires a sweep_plan output")
    out_path = ledger.output_path(workflow, node_id, "sweep_plan")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plan = [
        {
            "unit_id": unit_id,
            "child_recipe": child_recipe,
            "kickoff_hint": {
                "unit_id": unit_id,
                "reason": goal_state[unit_id].get("reason", ""),
                "evidence": evidence_map.get(unit_id, {}).get("text", ""),
                "evidence_path": evidence_map.get(unit_id, {}).get("path", ""),
            },
        }
        for unit_id in selected
    ]
    out_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out_path


def _harvest_selected_evidence(
    selected: list[str],
    goal_state: dict[str, Any],
) -> dict[str, dict[str, str]]:
    """Harvest MO_GOAL_EVIDENCE_CMD output for the selected units.

    Returns ``{unit_id: {"text": <inline evidence>, "path": <evidence file>}}``.
    Best-effort: a missing command, blank target cwd, or a harvest crash yields
    the predicate reason as a graceful fallback so the wave never blocks on the
    evidence stage. The full text is also written to
    ``<run_dir>/evidence/<slug>.md`` for the child to read in full.
    """
    evidence_cmd = (os.environ.get("MO_GOAL_EVIDENCE_CMD") or "").strip()
    if not evidence_cmd or not selected:
        return {}
    target_cwd = os.environ.get("MO_GOAL_TARGET_CWD") or os.getcwd()
    try:
        raw = harvest_evidence(target_cwd, evidence_cmd, selected)
    except Exception as exc:  # noqa: BLE001 — evidence is advisory, never fatal
        raw = {uid: f"[evidence harvest failed: {exc}]" for uid in selected}

    run_dir = os.environ.get("MINI_ORK_RUN_DIR")
    ev_dir = Path(run_dir) / "evidence" if run_dir else None
    if ev_dir is not None:
        ev_dir.mkdir(parents=True, exist_ok=True)

    out: dict[str, dict[str, str]] = {}
    for uid in selected:
        text = raw.get(uid, "") or goal_state.get(uid, {}).get("reason", "")
        path_str = ""
        if ev_dir is not None:
            ev_path = ev_dir / f"{_slug(uid)}.md"
            try:
                ev_path.write_text(text + "\n", encoding="utf-8")
                path_str = str(ev_path)
            except OSError:
                path_str = ""
        out[uid] = {"text": text, "path": path_str}
    return out


# ─────────────────────────────────────────────────────────────────────────
# goal_apply_deploy — the loop-closing node (deploy fix -> re-dispatch -> await)
# ─────────────────────────────────────────────────────────────────────────

# Sweep outcomes whose fix child actually ran against the target worktree, so a
# candidate patch may be present to deploy. ``deferred`` (spawn-cap) and
# ``dry_run`` never touched the tree. A ``failed`` child still commonly leaves a
# real patch behind (a later node — reviewer/eval — reddened the child while its
# earlier edit survived), so it is included; whether there is anything to ship is
# decided by MO_GOAL_APPLY_CMD (a clean tree makes the deploy a harmless no-op).
_APPLIED_SWEEP_STATUSES = frozenset({"spawned", "failed", "fanned_out"})


def _int_env(name: str, default: int) -> int:
    """Read an int env var; a blank/absent/garbage value falls back to default."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _run(cmd: list[str] | str, *, cwd: str | None, shell: bool) -> dict[str, Any]:
    """Run a command, capturing a truncated stdout/stderr receipt. Never raises."""
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, shell=shell, capture_output=True, text=True,
        )
        return {
            "cmd": cmd,
            "rc": proc.returncode,
            "stdout": (proc.stdout or "")[-4000:],
            "stderr": (proc.stderr or "")[-2000:],
        }
    except OSError as exc:
        return {"cmd": cmd, "rc": 127, "stdout": "", "stderr": f"exec failed: {exc}"}


def _poll_until_zero(
    run_fn: Callable[[], int], *, timeout_s: int, poll_s: int,
) -> dict[str, Any]:
    """Call ``run_fn`` until it returns 0 or ``timeout_s`` elapses.

    ``poll_s == 0`` (test seam) collapses to at most two probes: the wait
    accounting jumps past the timeout so the loop cannot spin CPU-hot forever
    on a predicate that never settles.
    """
    step = poll_s if poll_s > 0 else (timeout_s + 1)
    waited = 0
    last: int | None = None
    while True:
        last = run_fn()
        if last == 0:
            return {"ok": True, "waited_s": waited, "last_rc": last}
        if waited >= timeout_s:
            return {"ok": False, "waited_s": waited, "last_rc": last}
        if poll_s > 0:
            time.sleep(poll_s)
        waited += step


def _units_to_apply(run_dir: str) -> list[str]:
    """Read ``sweep-result.json`` and return the swept unit ids worth deploying."""
    sweep_path = Path(run_dir) / "sweep-result.json"
    if not sweep_path.is_file():
        return []
    try:
        data = json.loads(sweep_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    units = data.get("units", []) if isinstance(data, dict) else []
    out: list[str] = []
    for entry in units:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("status", "")) in _APPLIED_SWEEP_STATUSES:
            uid = entry.get("unit_id")
            if uid is not None:
                out.append(str(uid))
    return out


def _await_deploy() -> dict[str, Any]:
    """Block until the pushed fix is live on the target system.

    Two modes: poll ``MO_GOAL_AWAIT_DEPLOY_CMD`` until exit 0 (the caller's
    proof the new code is running), or — when that command is unset — sleep a
    fixed settle window that covers the target's auto-deploy latency.
    """
    cmd = os.environ.get("MO_GOAL_AWAIT_DEPLOY_CMD")
    if not cmd:
        settle = _int_env("MO_GOAL_DEPLOY_SETTLE_SECONDS", 600)
        if settle > 0:
            time.sleep(settle)
        return {"mode": "settle", "seconds": settle}
    timeout_s = _int_env("MO_GOAL_DEPLOY_TIMEOUT_SECONDS", 900)
    poll_s = _int_env("MO_GOAL_APPLY_POLL_SECONDS", 60)
    result = _poll_until_zero(
        lambda: _run(cmd, cwd=None, shell=True)["rc"],
        timeout_s=timeout_s, poll_s=poll_s,
    )
    return {"mode": "poll", "deployed": result["ok"], **result}


def _await_terminal(unit_id: str, cwd: str) -> dict[str, Any]:
    """Block until ``unit_id`` reaches a terminal state after re-dispatch.

    Polls ``MO_GOAL_TERMINAL_CMD`` (argv prefix + unit_id) when set, else falls
    back to ``MO_GOAL_PREDICATE_CMD`` (terminal == the goal predicate passes).
    The unit id is always the final argv slot — never shell-interpolated.
    """
    terminal_cmd = os.environ.get("MO_GOAL_TERMINAL_CMD") or os.environ.get(
        "MO_GOAL_PREDICATE_CMD",
    )
    if not terminal_cmd:
        return {"settled": False, "reason": "no MO_GOAL_TERMINAL_CMD/PREDICATE_CMD"}
    argv = shlex.split(terminal_cmd) + [unit_id]
    timeout_s = _int_env("MO_GOAL_APPLY_AWAIT_SECONDS", 5400)
    poll_s = _int_env("MO_GOAL_APPLY_POLL_SECONDS", 60)
    result = _poll_until_zero(
        lambda: _run(argv, cwd=cwd, shell=False)["rc"],
        timeout_s=timeout_s, poll_s=poll_s,
    )
    return {"settled": result["ok"], "unit_id": unit_id, **result}


def _run_apply(run_dir: str) -> dict[str, Any]:
    """Core of ``goal_apply_deploy`` — env-driven, ledger-free, unit-testable.

    Returns the ``apply-result.json`` payload. The transform wrapper only
    resolves the output path and writes the returned dict.
    """
    if os.environ.get("MO_GOAL_APPLY", "").strip() != "1":
        return {"status": "disabled", "units": []}

    target_cwd = os.environ.get("MO_GOAL_TARGET_CWD")
    apply_cmd = os.environ.get("MO_GOAL_APPLY_CMD")
    redispatch_cmd = os.environ.get("MO_GOAL_REDISPATCH_CMD")
    missing = [
        name
        for name, val in (
            ("MO_GOAL_TARGET_CWD", target_cwd),
            ("MO_GOAL_APPLY_CMD", apply_cmd),
            ("MO_GOAL_REDISPATCH_CMD", redispatch_cmd),
        )
        if not val
    ]
    if missing:
        raise ArtifactContractError(
            "goal_apply_deploy (MO_GOAL_APPLY=1) requires " + ", ".join(missing),
        )
    assert target_cwd and apply_cmd and redispatch_cmd  # narrowed for type-checkers

    units = _units_to_apply(run_dir)

    if os.environ.get("MO_GOAL_APPLY_DRY", "").strip() == "1":
        return {
            "status": "dry_run",
            "target_cwd": target_cwd,
            "deploy_cmd": apply_cmd,
            "redispatch_cmd": redispatch_cmd,
            "units": [
                {"unit_id": u, "would": ["deploy", "await_deploy", "redispatch", "await_regen"]}
                for u in units
            ],
        }

    # ── live: DEPLOY -> await-deploy -> REDISPATCH -> await-regen ──
    # Ordering is load-bearing: re-dispatching before the new code is live would
    # let the stale worker pick the unit up and re-fail with the same bug.
    deploy = _run(apply_cmd, cwd=target_cwd, shell=True)
    await_deploy = _await_deploy()
    unit_results: list[dict[str, Any]] = []
    for unit_id in units:
        redispatch = _run(
            shlex.split(redispatch_cmd) + [unit_id], cwd=target_cwd, shell=False,
        )
        settled = _await_terminal(unit_id, target_cwd)
        unit_results.append(
            {"unit_id": unit_id, "redispatch": redispatch, "await_regen": settled},
        )
    return {
        "status": "applied",
        "deploy": deploy,
        "await_deploy": await_deploy,
        "units": unit_results,
    }


@register_transform("goal_apply_deploy")
def goal_apply_deploy(workflow: CompiledWorkflow, ledger: ArtifactLedger, node_id: str) -> Path:
    """Deploy the sweep's fix, re-dispatch swept units, and await their terminal state.

    No-op passthrough (status ``disabled``) unless MO_GOAL_APPLY=1. See
    ``_run_apply`` for the full env contract; the wrapper only resolves the
    run-local ``apply-result.json`` output and persists the payload.
    """
    node = workflow.nodes[node_id]
    if "apply_result" not in node.outputs:
        raise ArtifactContractError("goal_apply_deploy requires an apply_result output")
    out_path = ledger.output_path(workflow, node_id, "apply_result")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    run_dir = os.environ.get("MINI_ORK_RUN_DIR", str(out_path.parent))
    payload = _run_apply(run_dir)
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out_path