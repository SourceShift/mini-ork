"""Cross-wave loop driver + per-wave sweep dispatcher for the goal-loop recipe.

This module exposes two surfaces:

1. ``drive()`` — the OUTER cross-wave driver (kickoff §Goal ¶2). Reads
   persistent ``goal-loop-state.json``, runs waves via an injectable
   ``run_wave_fn``, evaluates the UCCI stop conditions in the exact priority
   order ``goal_met > budget > diverged > all_quarantined``, and writes
   ``final-verdict.json`` on every stop path.

2. ``sweep_run()`` — the per-wave submode body registered by
   ``recipes/goal-loop/register.py``. Reads ``<run_dir>/sweep-plan.json``,
   fans out one ``mini_ork.cli.spawn.spawn(...)`` call per planned unit
   honoring ``MINI_ORK_RECURSIVE_MAX_PARALLEL``, and writes
   ``<run_dir>/sweep-result.json``. Catches ``ValueError`` from spawn caps
   and marks the unit ``deferred``. ``MO_GOAL_SPAWN_DRY=1`` records the
   planned spawns without invoking spawn (unit-test seam).

Entry-point semantics:
  - ``python3 drive.py`` (no args) → ``sweep_run()`` (dispatcher path).
  - ``python3 drive.py --goal-id ...`` → outer-loop CLI ``main()``.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

# File-path import pattern (recipes/ is not a package). Mirror
# mini_ork/cli/recipe_register.py:67.
_LOOP_STATE_PATH = Path(__file__).resolve().parent / "loop_state.py"
_loop_state_spec = importlib.util.spec_from_file_location(
    "goal_loop_loop_state", _LOOP_STATE_PATH,
)
if _loop_state_spec is None or _loop_state_spec.loader is None:
    raise ImportError(f"could not load loop_state helper from {_LOOP_STATE_PATH}")
_loop_state_module = importlib.util.module_from_spec(_loop_state_spec)
sys.modules.setdefault(_loop_state_spec.name, _loop_state_module)
_loop_state_spec.loader.exec_module(_loop_state_module)
load_state = _loop_state_module.load_state
save_state = _loop_state_module.save_state
record_wave = _loop_state_module.record_wave
should_quarantine = _loop_state_module.should_quarantine
divergence = _loop_state_module.divergence

FINAL_VERDICT_FILENAME = "final-verdict.json"


# ─────────────────────────────────────────────────────────────────────────
# Per-wave sweep submode body
# ─────────────────────────────────────────────────────────────────────────


def _default_spawn_fn(plan_entry: dict[str, Any]) -> dict[str, Any]:
    """Production spawn — invokes ``mini_ork.cli.spawn.spawn``.

    Honors ``MO_GOAL_SPAWN_DRY=1`` (dry-run) and the per-unit caps by letting
    the spawn API raise ``ValueError`` (caller marks ``deferred``).

    Per-unit kickoff templating (U4c): when ``kickoff_text`` is a file path
    AND contains no ``{{unit_id}}``/``{{reason}}`` placeholders, the original
    path is passed through unchanged. Otherwise the body is materialized
    per-unit into ``<run_dir>/_inline_kickoff_<slug>.md`` where ``slug`` is a
    filesystem-safe rendering of the unit id (unit ids are usually relative
    FILE PATHS containing ``/`` — the previous f-string filename silently broke
    on them).
    """
    if os.environ.get("MO_GOAL_SPAWN_DRY", "").strip() == "1":
        return {
            "status": "dry_run",
            "unit_id": plan_entry.get("unit_id"),
            "child_recipe": plan_entry.get("child_recipe", ""),
        }

    from mini_ork.cli.spawn import spawn  # lazy import keeps import-safe

    run_id = os.environ.get("MINI_ORK_RUN_ID", "")
    child_recipe = str(plan_entry.get("child_recipe", "") or "")
    kickoff_hint = plan_entry.get("kickoff_hint") or {}
    kickoff_text = kickoff_hint.get("kickoff") or os.environ.get(
        "MO_GOAL_CHILD_KICKOFF", "",
    )
    if not kickoff_text:
        raise ValueError(
            "sweep_run: child kickoff missing (set MO_GOAL_CHILD_KICKOFF "
            "or include kickoff_hint.kickoff in sweep-plan entry)"
        )

    is_file_source = os.path.isfile(kickoff_text)
    if is_file_source:
        body = Path(kickoff_text).read_text(encoding="utf-8")
    else:
        body = kickoff_text

    unit = str(plan_entry.get("unit_id", "") or "")
    reason = str((plan_entry.get("kickoff_hint") or {}).get("reason", "") or "")
    materialized = (
        body.replace("{{unit_id}}", unit).replace("{{reason}}", reason)
    )

    if is_file_source and materialized == body:
        # No placeholders + file source → pass the original file path
        # unchanged (zero-regression shortcut for the common static template).
        kickoff_path = kickoff_text
    else:
        run_dir = os.environ.get("MINI_ORK_RUN_DIR", ".")
        slug = re.sub(r"[^A-Za-z0-9._-]", "_", unit)[:80] or "x"
        kickoff_path = os.path.join(run_dir, f"_inline_kickoff_{slug}.md")
        Path(kickoff_path).write_text(materialized, encoding="utf-8")

    result = spawn(
        parent_run=run_id,
        kickoff=kickoff_path,
        recipe=child_recipe,
        allow_child_spawn=int(os.environ.get("MINI_ORK_ALLOW_CHILD_SPAWN", "0")),
        no_execute=int(os.environ.get("MO_GOAL_NO_EXECUTE", "0")),
    )
    return {
        "status": "spawned" if result.exit_code == 0 else "failed",
        "unit_id": plan_entry.get("unit_id"),
        "child_recipe": child_recipe,
        "spawn_id": result.spawn_id,
        "child_run_id": None,
        "exit_code": result.exit_code,
    }


def sweep_run(
    spawn_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> int:
    """Submode body — read sweep-plan.json, fan out spawns, write sweep-result.json.

    Returns 0 on success (even when individual spawns defer/fail; those are
    captured in ``sweep-result.json``). Non-zero rc only on contract violation
    (missing plan, unreadable file).
    """
    run_dir = os.environ.get("MINI_ORK_RUN_DIR", ".")
    plan_path = Path(run_dir) / "sweep-plan.json"
    result_path = Path(run_dir) / "sweep-result.json"
    resolved_spawn = spawn_fn or _default_spawn_fn

    if not plan_path.is_file():
        payload = {"status": "no_plan", "reason": f"{plan_path} missing", "units": []}
        result_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 0

    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        payload = {"status": "error", "reason": f"plan parse failed: {exc}", "units": []}
        result_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 1

    if not isinstance(plan, list):
        payload = {"status": "error", "reason": "sweep-plan must be a list", "units": []}
        result_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 1

    units: list[dict[str, Any]] = []
    for entry in plan:
        try:
            outcome = resolved_spawn(entry)
        except ValueError as exc:
            outcome = {
                "status": "deferred",
                "unit_id": entry.get("unit_id"),
                "child_recipe": entry.get("child_recipe", ""),
                "reason": str(exc),
            }
        except Exception as exc:  # noqa: BLE001 — non-cap errors are recorded, not raised
            outcome = {
                "status": "deferred",
                "unit_id": entry.get("unit_id"),
                "child_recipe": entry.get("child_recipe", ""),
                "reason": f"spawn crashed: {exc}",
            }
        # Ensure every entry has the contract fields the goal_check verifier
        # (downstream) expects.
        outcome.setdefault("unit_id", entry.get("unit_id"))
        outcome.setdefault("child_recipe", entry.get("child_recipe", ""))
        outcome.setdefault("status", "unknown")
        units.append(outcome)

    payload = {
        "status": "fanned_out",
        "units": units,
        "dry_run": os.environ.get("MO_GOAL_SPAWN_DRY", "").strip() == "1",
    }
    result_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


# ─────────────────────────────────────────────────────────────────────────
# Outer cross-wave driver
# ─────────────────────────────────────────────────────────────────────────


def _default_state_dir(goal_id: str) -> str:
    home = os.environ.get("MINI_ORK_HOME", ".mini-ork")
    return os.path.join(home, "goal-loop", goal_id)


def _default_run_wave_fn(wave_no: int, quarantined: set[str]) -> dict[str, Any]:
    """Production wave runner — shells ``bin/mini-ork run goal-loop <kickoff>``."""
    kickoff = os.environ.get("MO_GOAL_WAVE_KICKOFF") or os.environ.get("MO_GOAL_CHILD_KICKOFF")
    if not kickoff:
        raise RuntimeError(
            "MO_GOAL_WAVE_KICKOFF or MO_GOAL_CHILD_KICKOFF must be set for default run_wave_fn"
        )
    run_dir = os.environ.get("MINI_ORK_RUN_DIR")
    if not run_dir:
        raise RuntimeError("MINI_ORK_RUN_DIR must be set for default run_wave_fn")

    cli = os.path.join(
        os.environ.get("MINI_ORK_ROOT", "."), "bin", "mini-ork",
    )
    proc = subprocess.run(
        [cli, "run", "goal-loop", kickoff],
        check=False, capture_output=True, text=True,
    )
    panel_path = os.path.join(run_dir, "panel-verdict.json")
    payload: dict[str, Any] = {
        "wave": wave_no,
        "verdict": "fail",
        "failing_units": [],
        "total_units": 0,
        "exit_code": proc.returncode,
    }
    if os.path.isfile(panel_path):
        try:
            payload.update(json.loads(Path(panel_path).read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    payload["exit_code"] = proc.returncode
    payload["quarantined"] = sorted(quarantined)
    return payload


def _default_cost_fn() -> float:
    """Production cost reader — 24h rolling sum from ``mini_ork.scheduler``."""
    db = os.environ.get("MINI_ORK_DB")
    try:
        from mini_ork.scheduler import today_cost_usd
        return today_cost_usd(db)
    except Exception:
        return 0.0


def _projected_wave_cost(state: dict[str, Any]) -> float:
    """Mean of the last two recorded wave costs; 0.0 if fewer than 2 waves."""
    waves = state.get("waves", [])
    if len(waves) < 2:
        return 0.0
    costs = [float(w.get("cost_usd", 0.0)) for w in waves[-2:]]
    return sum(costs) / len(costs)


def _write_final_verdict(state_dir: str | Path, payload: dict[str, Any]) -> Path:
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    out = Path(state_dir) / FINAL_VERDICT_FILENAME
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def _env_int(name: str, default: int) -> int:
    """Read an int from the environment, falling back to ``default`` when unset.

    A set-but-unparseable value raises rather than silently defaulting: these are
    published by the executor from a validated ``recursion:`` block, so garbage
    here means something upstream is broken, and a quiet fallback would hide it.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    """Read a float from the environment, falling back to ``default`` when unset."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def drive(
    goal_id: str,
    target_cwd: str,
    units_cmd: str,
    predicate_cmd: str,
    child_recipe: str,
    *,
    max_waves: int | None = None,
    budget_total_usd: float | None = None,
    run_wave_fn: Callable[[int, set[str]], dict[str, Any]] | None = None,
    cost_fn: Callable[[], float] | None = None,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Run the outer cross-wave loop until a stop condition fires.

    The two caps resolve caller → declared → literal: an explicit argument wins,
    otherwise the recipe's ``recursion:`` block (published by the executor as
    ``MO_RECURSION_*``), otherwise the historical literal. So a recipe's YAML is
    the source of truth for its own loop, and a caller that passes nothing still
    gets today's behavior when no recipe declares a block.

    Returns the FINAL-VERDICT payload (also written to ``final-verdict.json``).
    Every stop path writes the file before returning.
    """
    if max_waves is None:
        max_waves = _env_int("MO_RECURSION_MAX_ITERATIONS", 30)
    if budget_total_usd is None:
        budget_total_usd = _env_float("MO_RECURSION_BUDGET_CAP_TOTAL_USD", 150.0)

    if max_waves <= 0:
        raise ValueError("max_waves must be > 0")

    resolved_state_dir = Path(state_dir) if state_dir is not None else Path(_default_state_dir(goal_id))
    resolved_run_wave = run_wave_fn or _default_run_wave_fn
    resolved_cost = cost_fn or _default_cost_fn

    state = load_state(resolved_state_dir, goal_id)

    while len(state.get("waves", [])) < max_waves:
        wave_no = len(state.get("waves", [])) + 1

        # GRAO quarantine: skip units whose last 2 fix-hashes are identical.
        quarantined: set[str] = set()
        for unit_id, history in state.get("failed_fixes", {}).items():
            if len(history) >= 2 and history[-1] == history[-2]:
                quarantined.add(unit_id)

        # Cheap projection-based budget stop BEFORE we spend on a new wave.
        projected = _projected_wave_cost(state)
        spent = float(resolved_cost())
        if projected > 0 and (spent + projected) > budget_total_usd:
            failing_now = state["waves"][-1]["failing_after"] if state["waves"] else []
            payload = {
                "stop": "budget",
                "waves": len(state["waves"]),
                "failing_units": failing_now,
                "quarantined_units": sorted(quarantined),
                "projected_next_cost": projected,
                "spent_usd": spent,
                "budget_total_usd": budget_total_usd,
            }
            _write_final_verdict(resolved_state_dir, payload)
            return payload

        verdict = resolved_run_wave(wave_no, quarantined)
        verdict_dict = verdict if isinstance(verdict, dict) else {}
        # Accept either the kickoff's panel-verdict.json key
        # (``failing_units``) or a richer test-synthesized key
        # (``failing_after``). ``failing_before`` defaults to the prior
        # wave's failing_after (the units the hunt tried to fix).
        if "failing_after" in verdict_dict:
            failing_after = sorted(verdict_dict.get("failing_after", []) or [])
        else:
            failing_after = sorted(verdict_dict.get("failing_units", []) or [])
        prev_waves = state.get("waves", [])
        prev_after = prev_waves[-1]["failing_after"] if prev_waves else []
        if "failing_before" in verdict_dict:
            failing_before = sorted(verdict_dict.get("failing_before", []) or [])
        else:
            failing_before = sorted(prev_after)
        cost_usd = float(verdict_dict.get("cost_usd", 0.0) or 0.0)
        run_id = str(verdict_dict.get("run_id", "") or "")

        record_wave(
            state,
            wave=wave_no,
            run_id=run_id,
            failing_before=failing_before,
            failing_after=failing_after,
            cost_usd=cost_usd,
        )

        # 1. goal_met — wave verdict == "pass".
        verdict_str = str(verdict_dict.get("verdict", "")).lower()
        if verdict_str == "pass":
            payload = {
                "stop": "goal_met",
                "waves": wave_no,
                "failing_units": [],
                "quarantined_units": sorted(quarantined),
            }
            save_state(state, resolved_state_dir)
            _write_final_verdict(resolved_state_dir, payload)
            return payload

        # 2. budget — cumulative OR projection check. The projection check
        # is what the kickoff means by "projected next-wave cost would
        # exceed budget_total_usd → stop budget": if running another wave
        # would push spend over budget, stop NOW rather than running it.
        # This must run BEFORE divergence per the kickoff's stop priority
        # (``budget > diverged``).
        spent = float(resolved_cost())
        projected = _projected_wave_cost(state)
        budget_stop = (
            spent >= budget_total_usd
            or (projected > 0 and (spent + projected) > budget_total_usd)
        )
        if budget_stop:
            payload = {
                "stop": "budget",
                "waves": wave_no,
                "failing_units": failing_after,
                "quarantined_units": sorted(quarantined),
                "spent_usd": spent,
                "projected_next_cost": projected,
                "budget_total_usd": budget_total_usd,
            }
            save_state(state, resolved_state_dir)
            _write_final_verdict(resolved_state_dir, payload)
            return payload

        # 3. diverged — UCCI divergence-kill (signature repeat or regressing).
        div = divergence(state)
        if div is not None:
            payload = {
                "stop": "diverged",
                "signature": div,
                "waves": wave_no,
                "failing_units": failing_after,
                "quarantined_units": sorted(quarantined),
            }
            save_state(state, resolved_state_dir)
            _write_final_verdict(resolved_state_dir, payload)
            return payload

        # 4. all_quarantined — every still-failing unit is GRAO-quarantined.
        if failing_after and all(u in quarantined for u in failing_after):
            payload = {
                "stop": "all_quarantined",
                "waves": wave_no,
                "failing_units": failing_after,
                "quarantined_units": sorted(quarantined),
            }
            save_state(state, resolved_state_dir)
            _write_final_verdict(resolved_state_dir, payload)
            return payload

        # No stop fired; persist state and continue.
        save_state(state, resolved_state_dir)

    # max_waves reached — soft "exhaustion" stop (safety case).
    final_failing = state["waves"][-1]["failing_after"] if state["waves"] else []
    payload = {
        "stop": "max_waves_reached",
        "waves": len(state["waves"]),
        "failing_units": final_failing,
        "quarantined_units": sorted(
            u for u, h in state.get("failed_fixes", {}).items()
            if len(h) >= 2 and h[-1] == h[-2]
        ),
    }
    save_state(state, resolved_state_dir)
    _write_final_verdict(resolved_state_dir, payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    """CLI entry — outer loop mode.

    ``python3 drive.py --goal-id ... --target-cwd ...`` runs the full driver.
    Without args, the script falls back to ``sweep_run()`` (the dispatcher
    path; see module docstring).
    """
    if argv is None or (isinstance(argv, list) and len(argv) == 0):
        return sweep_run()
    if isinstance(argv, list) and argv and argv[0] == "--sweep":
        return sweep_run()

    parser = argparse.ArgumentParser(
        prog="goal-loop-driver",
        description="Outer cross-wave loop driver for the goal-loop recipe.",
    )
    parser.add_argument("--goal-id", required=True)
    parser.add_argument("--target-cwd", required=True)
    parser.add_argument("--units-cmd", required=True)
    parser.add_argument("--predicate-cmd", required=True)
    parser.add_argument("--child-recipe", required=True)
    parser.add_argument(
        "--max-waves", type=int, default=None,
        help="Default: the recipe's recursion.max_iterations, else 30",
    )
    parser.add_argument(
        "--budget-usd", type=float, default=None,
        help="Default: the recipe's recursion.budget_cap_total_usd, else 150.0",
    )
    parser.add_argument("--state-dir", default=None,
                        help="Default: ${MINI_ORK_HOME}/goal-loop/<goal-id>/")
    args = parser.parse_args(argv)

    try:
        verdict = drive(
            goal_id=args.goal_id,
            target_cwd=args.target_cwd,
            units_cmd=args.units_cmd,
            predicate_cmd=args.predicate_cmd,
            child_recipe=args.child_recipe,
            max_waves=args.max_waves,
            budget_total_usd=args.budget_usd,
            state_dir=args.state_dir,
        )
    except Exception as exc:
        sys.stderr.write(f"driver crashed: {exc}\n")
        return 2

    sys.stdout.write(json.dumps(verdict, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))