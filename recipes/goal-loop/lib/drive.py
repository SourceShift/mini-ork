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
import time
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
evidence_informativeness = _loop_state_module.evidence_informativeness
goal_vacuity = _loop_state_module.goal_vacuity
parse_obligations = _loop_state_module.parse_obligations
obligation_gap = _loop_state_module.obligation_gap


def _load_sibling(name: str):
    """Import a sibling module by path (recipes/ is not a package)."""
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"goal_loop_{name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {name} helper from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


_assurance = _load_sibling("assurance")
shield = _assurance.shield
resolve_shield_mode = _assurance.resolve_mode
_ledger = _load_sibling("loop_ledger")
append_decision = _ledger.append_decision
_goal_state = _load_sibling("goal_state")
read_obligations = _goal_state.read_obligations

FINAL_VERDICT_FILENAME = "final-verdict.json"


# ─────────────────────────────────────────────────────────────────────────
# Per-wave sweep submode body
# ─────────────────────────────────────────────────────────────────────────


def _child_diagnostics(child_run_dir: str) -> dict[str, Any]:
    """Summarize a finished child's run dir for the outer loop.

    The sweep entry is the only channel from child to driver, and it carried an
    exit code and nothing else — so a wave that spawned, changed nothing and was
    approved read exactly like one that did real work. These fields are what
    ``MO_GOAL_EVIDENCE_CMD`` templates into the next kickoff, so the next child
    can see whether its predecessor produced a diff at all.
    """
    out: dict[str, Any] = {}
    if not child_run_dir or not os.path.isdir(child_run_dir):
        return out
    verdict_path = os.path.join(child_run_dir, "verdict.json")
    if os.path.isfile(verdict_path):
        try:
            data = json.loads(Path(verdict_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            out["child_verdict"] = data.get("verdict", "")
            out["child_failed_nodes"] = data.get("failed_nodes")
    diff_path = os.path.join(child_run_dir, "review-diff.patch")
    if os.path.isfile(diff_path):
        try:
            out["review_diff_bytes"] = os.path.getsize(diff_path)
        except OSError:
            pass
    if os.path.isfile(os.path.join(child_run_dir, "review-diff-noop.json")):
        out["child_no_op"] = True
    return out


def _last_measured_failing(state: dict[str, Any]) -> list[str]:
    """The failing set from the newest wave that actually MEASURED one.

    A wave whose verdict never arrived records ``verdict_known: False``: its
    failing set is unobserved, not empty. Reading it as empty would both report
    a green the loop never saw and hand the next wave nothing to dispatch — the
    two ways a dead wave propagates into the campaign's decisions.
    """
    for wave in reversed(state.get("waves", [])):
        if wave.get("verdict_known", True):
            return sorted(wave.get("failing_after") or [])
    return []


def _unmeasured_waves(state: dict[str, Any]) -> list[int]:
    """Wave numbers whose verdict never arrived — the waves the loop could not read."""
    return [
        int(w["wave"])
        for w in state.get("waves", [])
        if w.get("wave") is not None and not w.get("verdict_known", True)
    ]


def _unknown_fragment(state: dict[str, Any]) -> dict[str, Any]:
    """Final-verdict fragment naming the waves that produced no measurement.

    Additive and omitted when empty, so a campaign whose every wave reported
    writes the byte-identical verdict it wrote before. When present it is the
    operator's cue that ``failing_units`` is the last OBSERVED set, not this
    wave's — the loop stopped on a number it did not measure.
    """
    unmeasured = _unmeasured_waves(state)
    return {"unmeasured_waves": unmeasured} if unmeasured else {}


def _wave_history(state: dict[str, Any], limit: int = 4) -> list[dict[str, Any]]:
    """Compact per-wave digest handed to the next wave's evidence harvest."""
    out: list[dict[str, Any]] = []
    for w in state.get("waves", [])[-limit:]:
        cd = w.get("child_diagnostics") or {}
        out.append({
            "wave": w.get("wave"),
            "attempted": w.get("attempted", []),
            "failing_after": w.get("failing_after", []),
            "headroom_closed": w.get("headroom_closed"),
            "predicate_moved": w.get("predicate_moved"),
            "child_verdict": sorted({str((v or {}).get("child_verdict")) for v in cd.values()}),
            "review_diff_bytes": sorted(
                {str((v or {}).get("review_diff_bytes")) for v in cd.values()}
            ),
        })
    return out


def _default_spawn_fn(plan_entry: dict[str, Any]) -> dict[str, Any]:
    """Production spawn — invokes ``mini_ork.cli.spawn.spawn``.

    Honors ``MO_GOAL_SPAWN_DRY=1`` (dry-run) and the per-unit caps by letting
    the spawn API raise ``ValueError`` (caller marks ``deferred``).

    Per-unit kickoff templating (U4c): when ``kickoff_text`` is a file path
    AND contains no ``{{unit_id}}``/``{{reason}}``/``{{evidence}}``/
    ``{{evidence_path}}`` placeholders, the original path is passed through
    unchanged. Otherwise the body is materialized per-unit into
    ``<run_dir>/_inline_kickoff_<slug>.md`` where ``slug`` is a filesystem-safe
    rendering of the unit id (unit ids are usually relative FILE PATHS
    containing ``/`` — the previous f-string filename silently broke on them).
    ``{{evidence}}`` inlines the deep failure signal harvested by
    ``MO_GOAL_EVIDENCE_CMD`` (falling back to the one-line reason); the child can
    also re-read the full, uncapped evidence at ``{{evidence_path}}``.
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

    hint = plan_entry.get("kickoff_hint") or {}
    unit = str(plan_entry.get("unit_id", "") or "")
    reason = str(hint.get("reason", "") or "")
    evidence_path = str(hint.get("evidence_path", "") or "")
    # {{evidence}} carries the deep, per-unit failure signal harvested by
    # MO_GOAL_EVIDENCE_CMD (produced-vs-required diff, failing node, log tail).
    # It falls back to the one-line reason when no evidence was harvested, so a
    # kickoff that references {{evidence}} always renders something actionable.
    evidence = str(hint.get("evidence", "") or "") or reason
    materialized = (
        body.replace("{{unit_id}}", unit)
        .replace("{{reason}}", reason)
        .replace("{{evidence_path}}", evidence_path)
        .replace("{{evidence}}", evidence)
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
    entry = {
        "status": "spawned" if result.exit_code == 0 else "failed",
        "unit_id": plan_entry.get("unit_id"),
        "child_recipe": child_recipe,
        "spawn_id": result.spawn_id,
        "child_run_id": result.child_run_id or None,
        "exit_code": result.exit_code,
    }
    entry.update(_child_diagnostics(getattr(result, "child_run_dir", "")))
    return entry


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
    # Propagate the GRAO quarantine set into the wave recipe so goal_sweep_plan
    # EXCLUDES units the loop has given up on. Without this a single-child-per-
    # wave loop re-selects the same stuck unit every wave (sorted-first) and
    # never rotates the freed slot onto the other failing units. Newline-
    # delimited; empty (no quarantine yet) leaves selection at historical.
    wave_env = dict(os.environ)
    wave_env["MO_GOAL_QUARANTINED_UNITS"] = "\n".join(sorted(quarantined))
    # Meta^n conditioning at depth 1: tell the child what its predecessors
    # already tried so it does not re-issue the same patch. Bounded to the last
    # 4 waves; rendered in transforms._harvest_selected_evidence.
    hist = os.environ.get("MO_GOAL_WAVE_HISTORY")
    if hist:
        wave_env["MO_GOAL_WAVE_HISTORY"] = hist
    # Real per-wave spend = the 24h-rolling cost meter's delta across the wave
    # subprocess. panel-verdict.json carries only the panel node's own cost (~$0),
    # NOT the fix child's dispatch spend, so folding it in as cost_usd left every
    # wave reading $0 — which blinds the autoraise predictor (never sees a FUNDED
    # wave → never stops) and _projected_wave_cost. Snapshot before/after instead.
    cost_before = _default_cost_fn()
    # panel-verdict.json lives in the RUN dir, which every wave of a campaign
    # shares. A wave that dies before writing one would otherwise be scored on
    # whatever the PREVIOUS wave left there — a stale verdict read as this wave's
    # measurement. Clear it first, so the file the wave is scored on can only
    # have been written by the wave itself, and so its absence is a real
    # no-verdict rather than a leftover.
    panel_path = os.path.join(run_dir, "panel-verdict.json")
    try:
        os.remove(panel_path)
    except OSError:
        pass
    # The wave is a long-lived subprocess with no other bound, and it can finish
    # its work and then hang in teardown: observed 2026-09-20, a wave wrote
    # sweep-result.json and then blocked forever acquiring a lock inside a
    # generator, so ``subprocess.run`` sat on its pipes and the driver never
    # advanced to another wave — the whole loop stalled ~47 min until the child
    # was killed by hand. Bound it so a hung wave is reaped and folded as a
    # FAILED wave, letting the loop keep going. On POSIX ``subprocess.run``'s
    # timeout SIGKILLs and ``waitpid``s the direct child (no pipe drain, so a
    # grandchild holding the write end cannot re-block the driver here) — but
    # it does NOT reap the wave's own descendants, which may outlive the wave.
    wave_timeout = float(os.environ.get("MO_GOAL_WAVE_TIMEOUT_SECONDS") or 5400)
    try:
        proc = subprocess.run(
            [cli, "run", "goal-loop", kickoff],
            check=False, capture_output=True, text=True, env=wave_env,
            timeout=wave_timeout,
        )
        wave_exit = proc.returncode
        wave_timed_out = False
    except subprocess.TimeoutExpired:
        # The child is already killed and reaped by ``subprocess.run``, so the
        # run-local outputs read below are whatever the wave managed to write
        # before it hung — a partial wave, scored as a failed one.
        wave_exit = -1
        wave_timed_out = True
    cost_after = _default_cost_fn()
    # ``failing_units`` is deliberately ABSENT from the defaults. A wave that
    # writes no panel has not measured its units, and defaulting the key to []
    # made "never measured" indistinguishable from a measured "nothing failing":
    # the folded zero then read as a satisfied goal, and the NEXT wave's real
    # count read as a regression from it. ``verdict: fail`` says the wave failed
    # as a wave; the missing key says we cannot say what it left behind, which
    # ``drive`` folds into ``verdict_known: False``.
    payload: dict[str, Any] = {
        "wave": wave_no,
        "verdict": "fail",
        "total_units": 0,
        "exit_code": wave_exit,
        "timed_out": wave_timed_out,
    }
    if os.path.isfile(panel_path):
        try:
            payload.update(json.loads(Path(panel_path).read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    # Per-unit failure fingerprints (progress signal) from goal-state.json:
    # a ``unit_id -> reason`` map the driver folds into the wave signature so
    # a fix that moves a unit's failure counts as progress even before the
    # failing SET shrinks.
    gs_path = os.path.join(run_dir, "goal-state.json")
    if os.path.isfile(gs_path):
        try:
            gs = json.loads(Path(gs_path).read_text(encoding="utf-8"))
            if isinstance(gs, dict):
                payload["unit_reasons"] = {
                    str(uid): str(v.get("reason", ""))
                    for uid, v in gs.items()
                    if isinstance(v, dict)
                }
                payload["unit_reproduced"] = {
                    str(uid): bool(v.get("reproduced", True))
                    for uid, v in gs.items()
                    if isinstance(v, dict)
                }
        except json.JSONDecodeError:
            pass

    # Which units this wave actually dispatched a fix for (sweep fan-out) —
    # scopes GRAO quarantine so a still-pending unit the loop never reached
    # does not accrue identical hashes and trip ``all_quarantined``.
    sw_path = os.path.join(run_dir, "sweep-result.json")
    if os.path.isfile(sw_path):
        try:
            sw = json.loads(Path(sw_path).read_text(encoding="utf-8"))
            attempted = [
                str(u.get("unit_id"))
                for u in sw.get("units", [])
                if isinstance(u, dict) and u.get("unit_id") is not None
            ]
            if attempted:
                payload["attempted"] = attempted
            diag = {
                str(u.get("unit_id")): {
                    k: u.get(k)
                    for k in ("child_verdict", "review_diff_bytes", "child_no_op",
                              "child_failed_nodes", "child_run_id")
                    if u.get(k) is not None
                }
                for u in sw.get("units", [])
                if isinstance(u, dict) and u.get("unit_id") is not None
            }
            diag = {k: v for k, v in diag.items() if v}
            if diag:
                payload["child_diagnostics"] = diag
        except json.JSONDecodeError:
            pass

    # Evidence fingerprints — the bundle this wave's children were handed.
    # Joined to the next wave's predicate delta, this is the loop's r_disc.
    sp_path = os.path.join(run_dir, "sweep-plan.json")
    if os.path.isfile(sp_path):
        try:
            sp = json.loads(Path(sp_path).read_text(encoding="utf-8"))
            ev = {
                str(e.get("unit_id")): str(e.get("evidence_sha", ""))
                for e in sp
                if isinstance(e, dict) and e.get("unit_id") is not None and e.get("evidence_sha")
            }
            if ev:
                payload["evidence"] = ev
            # The operator CLASS each unit's failure called for. SHADOW: the
            # sweep still spawned `child_recipe`; this records what a typed
            # action set WOULD have chosen, so its choices can be graded against
            # the predicate delta before it is ever permitted to dispatch.
            op = {
                str(e.get("unit_id")): str(e.get("operator", ""))
                for e in sp
                if isinstance(e, dict) and e.get("unit_id") is not None and e.get("operator")
            }
            if op:
                payload["operators"] = op
        except (json.JSONDecodeError, OSError):
            pass

    # Override any panel-sourced cost with the measured spend delta. Clamp at 0:
    # a non-positive delta means the wave spent nothing measurable (circuit-
    # starved) or the 24h window slid — either way "not funded", which is what
    # the predictor must read. A funded wave's delta is unambiguously positive.
    wave_cost = cost_after - cost_before
    payload["cost_usd"] = wave_cost if wave_cost > 0 else 0.0
    payload["exit_code"] = wave_exit
    payload["timed_out"] = wave_timed_out
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


def _goal_diagnostics(
    state: dict[str, Any],
    reasons: dict[str, str] | None,
    target_cwd: str,
) -> dict[str, Any]:
    """What the goal's green banner did NOT establish.

    Runs ONLY on the pass path, where ``divergence()`` is unreachable — the
    driver returns ``goal_met`` first. Answers a different question than the
    give-up detectors: not "did the loop stall?" but "did the loop look at
    enough to know?". Two independent probes:

      * vacuity — did any axis the predicate reports ever discriminate? A book
        whose every axis reads ``unset``/``0`` was passed against nothing.
      * obligations — does the target declare a duty the predicate has no axis
        for? The sensor is operator-seeded (``MO_GOAL_OBLIGATION_CMD``); when
        configured and it fails, the failure is recorded, never read as "none".

    Purely diagnostic — each finding is a sub-key, attached only when present,
    mirroring ``record_wave``'s optional-key contract. It never changes ``stop``.
    """
    diag: dict[str, Any] = {}
    vacuity = goal_vacuity(state, reasons)
    if vacuity:
        diag["vacuity"] = vacuity

    obligations_cmd = os.environ.get("MO_GOAL_OBLIGATION_CMD", "").strip()
    if obligations_cmd:
        text, err = read_obligations(target_cwd, obligations_cmd)
        if err:
            diag["obligation_error"] = err
        rows = parse_obligations(text)
        if rows:
            diag["obligations"] = rows
            gap = obligation_gap(rows)
            if gap:
                diag["obligation_gap"] = gap
    return diag


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


def _env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean flag from the environment (``1/true/yes/on`` → True)."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _daily_budget_now() -> float:
    """The GLOBAL 24h cost-circuit budget the wave's child will enforce.

    Mirrors ``mini_ork.dispatch.llm_dispatch.cost_circuit_open``'s knob
    (``MO_DAILY_BUDGET_USD``, default 50) — a rolling-24h spend ceiling checked
    on every LLM call INSIDE the wave/child process. This is SEPARATE from the
    driver's own cumulative ``budget_total_usd`` cap: the driver can be nowhere
    near its $300 total yet still have every child starved by a $50 daily rail.
    """
    return _env_float("MO_DAILY_BUDGET_USD", 50.0)


def _raise_predicted_helpful(state: dict[str, Any], patience: int) -> bool:
    """Predict whether lifting the daily budget will actually move the goal.

    Reuses the loop's own progress fingerprint (per-wave ``signature``) — but on
    FUNDED evidence only. A circuit-starved wave spends ~$0 and re-emits the same
    signature (empty diff → unchanged failure), which naively reads as
    stagnation and would wrongly veto the raise. Counting only waves that spent
    >= ``MO_GOAL_FUNDED_WAVE_MIN_USD`` means flat fingerprints from starved waves
    cannot masquerade as "stuck": we simply have not tried with budget yet, so
    the raise deserves a chance. Once ``patience`` FUNDED waves all share one
    signature, the fixer HAD money and still did not move the failure → more
    budget will not help → predict unhelpful (the caller then stops).
    """
    min_funded = _env_float("MO_GOAL_FUNDED_WAVE_MIN_USD", 1.0)
    funded = [
        w for w in state.get("waves", [])
        if float(w.get("cost_usd", 0.0) or 0.0) >= min_funded
        # A wave that never reported has no signature to compare — counting it
        # would read as a moved fingerprint (None != sha) and buy a raise on a
        # measurement that does not exist.
        and w.get("verdict_known", True)
    ]
    if len(funded) < max(2, patience):
        return True  # not enough FUNDED evidence to call it genuinely stuck
    sigs = [w.get("signature") for w in funded[-patience:]]
    # Progress = the fingerprint moved at least once across the funded window.
    return not (sigs[0] is not None and all(s == sigs[0] for s in sigs))


def _budget_autoraise_decision(
    state: dict[str, Any],
    spent: float,
    patience: int,
    budget_total_usd: float,
) -> tuple[str, float | str]:
    """Decide the daily-budget action BEFORE spending on the next wave.

    Returns exactly one of:
      ``("noop", cur)``      the circuit will not starve the next wave — leave it.
      ``("raise", target)``  lift ``MO_DAILY_BUDGET_USD`` to ``target`` (progress predicted).
      ``("stop", reason)``   a raise is needed but predicted NOT to help → stop.

    The cap defaults to the driver's own ``budget_total_usd`` so the daily
    circuit is never lifted above the sanctioned cumulative budget; the loop's
    existing cumulative/projection budget stop stays the hard ceiling above this.
    """
    cur = _daily_budget_now()
    projected = _projected_wave_cost(state)  # 0.0 when < 2 waves recorded
    need = spent + (projected if projected > 0 else 0.0)
    # Will the NEXT wave trip the circuit? (already-over spend alone is enough.)
    if need < cur and spent < cur:
        return ("noop", cur)
    cap = _env_float("MO_GOAL_BUDGET_AUTORAISE_CAP", budget_total_usd)
    headroom = _env_float("MO_GOAL_BUDGET_AUTORAISE_HEADROOM", 5.0)
    target = min(cap, need + headroom)
    if not _raise_predicted_helpful(state, patience):
        return ("stop", "no_predicted_progress")
    if target <= cur:
        # Want to continue, but the cap is the wall — honor the ceiling, stop.
        return ("stop", f"cap_reached:{cap:.2f}")
    return ("raise", target)


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

    # Patience windows for the two give-up detectors. Both default to 2 (a
    # single repeat is terminal — correct for a loop that attempts every
    # failing unit each wave). A per-unit loop over N units with one fix
    # attempt per wave sets these higher so the fixer gets several waves to
    # move a stuck unit before the loop declares divergence / quarantines it.
    divergence_patience = max(2, _env_int("MO_GOAL_DIVERGENCE_PATIENCE", 2))
    quarantine_patience = max(2, _env_int("MO_GOAL_QUARANTINE_PATIENCE", 2))
    # How many FUNDED waves the autoraise predictor waits on before calling a
    # unit un-raisable (defaults to the divergence window). Decoupled so the
    # budget "raise won't help → stop" gate can fire on funded-flat evidence
    # even when divergence is set loose — and so the two stops don't collide on
    # one shared window (divergence, checked post-wave, would always pre-empt it).
    autoraise_patience = max(2, _env_int("MO_GOAL_AUTORAISE_PATIENCE", divergence_patience))

    resolved_state_dir = Path(state_dir) if state_dir is not None else Path(_default_state_dir(goal_id))
    resolved_run_wave = run_wave_fn or _default_run_wave_fn
    resolved_cost = cost_fn or _default_cost_fn

    state = load_state(resolved_state_dir, goal_id)

    while len(state.get("waves", [])) < max_waves:
        wave_no = len(state.get("waves", [])) + 1

        # GRAO quarantine: skip units whose last ``quarantine_patience``
        # fix-hashes are all identical (fixer produced the same-fingerprint
        # result that many attempts running).
        quarantined: set[str] = set()
        for unit_id, history in state.get("failed_fixes", {}).items():
            tail = history[-quarantine_patience:]
            if len(tail) >= quarantine_patience and len(set(tail)) == 1:
                quarantined.add(unit_id)

        # RSI daily-budget autoraise (opt-in MO_GOAL_BUDGET_AUTORAISE): the
        # wave's code-fix child enforces the GLOBAL 24h cost circuit
        # (MO_DAILY_BUDGET_USD), SEPARATE from this driver's cumulative
        # budget_total_usd. Lift it when the circuit would otherwise starve the
        # next wave — but only when progress is predicted. If a raise is
        # predicted NOT to help (a fixer that HAD budget and still could not move
        # the failure across the patience window), STOP rather than burn money.
        # Runs BEFORE the wave dispatch so the raised value is in os.environ when
        # _default_run_wave_fn snapshots wave_env for the child subprocess.
        if _env_bool("MO_GOAL_BUDGET_AUTORAISE"):
            spent_now = float(resolved_cost())
            action, value = _budget_autoraise_decision(
                state, spent_now, autoraise_patience, budget_total_usd
            )
            if action == "stop":
                failing_now = _last_measured_failing(state)
                payload = {
                    "stop": "budget_autoraise",
                    "reason": value,
                    "waves": len(state["waves"]),
                    "failing_units": failing_now,
                    "quarantined_units": sorted(quarantined),
                    "spent_usd": spent_now,
                    "daily_budget_usd": _daily_budget_now(),
                }
                _write_final_verdict(resolved_state_dir, payload)
                return payload
            if action == "raise":
                os.environ["MO_DAILY_BUDGET_USD"] = f"{float(value):.2f}"

        # Cheap projection-based budget stop BEFORE we spend on a new wave.
        projected = _projected_wave_cost(state)
        spent = float(resolved_cost())
        if projected > 0 and (spent + projected) > budget_total_usd:
            failing_now = _last_measured_failing(state)
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

        # Re-target, don't just stop: when the evidence we keep feeding is not
        # moving the predicate, tell the next wave's harvest to lead with the
        # wave history instead of re-issuing the same bundle.
        if _env_bool("MO_GOAL_RDISC", True):
            uninformative = evidence_informativeness(state, divergence_patience)
            if uninformative:
                os.environ["MO_GOAL_EVIDENCE_UNINFORMATIVE"] = "1"
            else:
                os.environ.pop("MO_GOAL_EVIDENCE_UNINFORMATIVE", None)
            os.environ["MO_GOAL_WAVE_HISTORY"] = json.dumps(
                _wave_history(state, limit=4), sort_keys=True,
            )

        # Snapshot the decision the loop is ABOUT to make, before it makes it.
        # Read after the fact these numbers are unrecoverable — spend has moved
        # and the wave has overwritten the run artifacts — so the context half
        # of the ledger has to be captured here or not at all. The action half
        # is filled in below, once the sweep has named the units it dispatched.
        shield_mode = resolve_shield_mode()
        decision_ts = int(time.time())
        probed_before = _last_measured_failing(state)
        decision_context: dict[str, Any] = {
            "goal_id": goal_id,
            "wave": wave_no,
            "freshly_probed": probed_before,
            "quarantined": sorted(quarantined),
            "spent_usd": spent,
            "projected_wave_usd": projected,
            "budget_total_usd": budget_total_usd,
            "max_waves": max_waves,
            "waves_elapsed": len(state.get("waves", [])),
            "uninformative_evidence": os.environ.get("MO_GOAL_EVIDENCE_UNINFORMATIVE") == "1",
            "divergence": divergence(state, patience=divergence_patience),
        }
        decision_action: dict[str, Any] = {
            "kind": "spawn_children",
            "child_recipe": child_recipe,
            "units": probed_before,
            "destructive": _env_bool("MO_GOAL_ALLOW_DESTRUCTIVE", False),
        }

        verdict = resolved_run_wave(wave_no, quarantined)
        verdict_dict = verdict if isinstance(verdict, dict) else {}
        # Accept either the kickoff's panel-verdict.json key
        # (``failing_units``) or a richer test-synthesized key
        # (``failing_after``). ``failing_before`` defaults to the prior
        # wave's failing_after (the units the hunt tried to fix).
        #
        # A wave's failing set is KNOWN only when the verdict NAMED it. No key
        # means no measurement was taken: the wave timed out, crashed, or its
        # verifier emitted an ``error`` panel (goal_check writes no unit list on
        # that path). Folding the absence as an empty set is how a dead wave got
        # read as a green one — and how the next wave's real count got read as a
        # regression from zero, which killed the campaign.
        verdict_known = "failing_after" in verdict_dict or "failing_units" in verdict_dict
        if "failing_after" in verdict_dict:
            failing_after = sorted(verdict_dict.get("failing_after", []) or [])
        elif "failing_units" in verdict_dict:
            failing_after = sorted(verdict_dict.get("failing_units", []) or [])
        else:
            failing_after = []
        # What the loop last actually OBSERVED: this wave's own set when it
        # measured one, otherwise the newest wave that did. A stop report must
        # never present an unobserved empty set as the loop's failing units.
        failing_now = failing_after if verdict_known else _last_measured_failing(state)
        prev_after = _last_measured_failing(state)
        if "failing_before" in verdict_dict:
            failing_before = sorted(verdict_dict.get("failing_before", []) or [])
        else:
            failing_before = sorted(prev_after)
        cost_usd = float(verdict_dict.get("cost_usd", 0.0) or 0.0)
        run_id = str(verdict_dict.get("run_id", "") or "")

        # Per-unit failure fingerprints (goal-state.json) + the units the wave
        # actually attempted (sweep fan-out) sharpen the give-up detectors so a
        # single-child-per-wave loop reads real progress, not set churn.
        raw_reasons = verdict_dict.get("unit_reasons")
        unit_reasons = (
            {str(k): str(v) for k, v in raw_reasons.items()}
            if isinstance(raw_reasons, dict)
            else None
        )
        raw_reproduced = verdict_dict.get("unit_reproduced")
        unit_reproduced = (
            {str(k): bool(v) for k, v in raw_reproduced.items()}
            if isinstance(raw_reproduced, dict) else None
        )
        raw_attempted = verdict_dict.get("attempted")
        attempted = (
            [str(u) for u in raw_attempted]
            if isinstance(raw_attempted, list)
            else None
        )
        raw_evidence = verdict_dict.get("evidence")
        raw_child = verdict_dict.get("child_diagnostics")
        raw_operators = verdict_dict.get("operators")
        diagnostics = None
        if (
            isinstance(raw_evidence, dict)
            or isinstance(raw_child, dict)
            or isinstance(raw_operators, dict)
        ):
            diagnostics = {
                "evidence": raw_evidence if isinstance(raw_evidence, dict) else {},
                "child_diagnostics": raw_child if isinstance(raw_child, dict) else {},
                "operators": raw_operators if isinstance(raw_operators, dict) else {},
            }

        record_wave(
            state,
            wave=wave_no,
            run_id=run_id,
            failing_before=failing_before,
            failing_after=failing_after,
            cost_usd=cost_usd,
            reasons=unit_reasons,
            attempted=attempted,
            diagnostics=diagnostics,
            verdict_known=verdict_known,
        )

        # Record the DECISION, not just its outcome. ``record_wave`` above keeps
        # what the loop achieved; this keeps what it knew, what it chose, and
        # whether the assurance shield would have permitted the choice. The
        # evidence fingerprints are read back off the wave record rather than
        # re-derived, so the ledger and the state file can never disagree about
        # which bundle a wave fed.
        last_wave = state["waves"][-1]
        prev_wave = state["waves"][-2] if len(state["waves"]) > 1 else {}
        decision_action["units"] = sorted(attempted) if attempted else probed_before
        decision_context["evidence_sha"] = {
            str(k): str(v) for k, v in (last_wave.get("evidence") or {}).items()
        }
        decision_context["prev_evidence_sha"] = {
            str(k): str(v) for k, v in (prev_wave.get("evidence") or {}).items()
        }
        decision_context["predicate_moved"] = bool(last_wave.get("predicate_moved"))
        # The operator CLASS the loop recorded per unit. SHADOW: `child_recipe`
        # above is what actually spawned; this is what a typed action set WOULD
        # have chosen. Recording both is the point — the ledger can then answer
        # "did the class we were forced to use match the class the failure called
        # for, and what did that cost?" without a second data source.
        decision_action["operators"] = {
            str(k): str(v) for k, v in (last_wave.get("operators") or {}).items()
        }
        shield_verdict = (
            {"allow": True, "guard": None, "reason": "shield off"}
            if shield_mode == "off"
            else shield(decision_action, decision_context)
        )
        if shield_mode != "off":
            append_decision(
                resolved_state_dir,
                {
                    "goal_id": goal_id,
                    "wave": wave_no,
                    "context": decision_context,
                    "action": decision_action,
                    "outcome": {
                        "failing_after": failing_now,
                        "verdict_known": verdict_known,
                        "attempted": decision_action["units"],
                        "headroom_closed": (
                            len(failing_before) - len(failing_after)
                            if verdict_known else None
                        ),
                        "predicate_moved": last_wave.get("predicate_moved"),
                        "child_diagnostics": last_wave.get("child_diagnostics") or {},
                        "cost_usd": cost_usd,
                        "run_id": run_id,
                    },
                    "shield": shield_verdict,
                },
                ts=decision_ts,
            )
        if shield_mode == "enforce" and not shield_verdict["allow"]:
            payload = {
                "stop": "shield",
                "guard": shield_verdict["guard"],
                "reason": shield_verdict["reason"],
                "waves": wave_no,
                "failing_units": failing_now,
                "quarantined_units": sorted(quarantined),
            }
            save_state(state, resolved_state_dir)
            _write_final_verdict(resolved_state_dir, payload)
            return payload

        # 1. goal_met — wave verdict == "pass".
        verdict_str = str(verdict_dict.get("verdict", "")).lower()
        if verdict_str == "pass":
            payload = {
                "stop": "goal_met",
                "waves": wave_no,
                "failing_units": [],
                "quarantined_units": sorted(quarantined),
            }
            # Additive diagnostics: the green stands, but we record what it did
            # NOT establish (degenerate axes, declared-but-unsatisfied
            # obligations). Never rewrites ``stop`` — downstream switches on it.
            if _env_bool("MO_GOAL_VACUITY", True):
                diag = _goal_diagnostics(state, unit_reasons, target_cwd)
                if diag:
                    payload["diagnostics"] = diag
                    try:
                        append_decision(
                            resolved_state_dir,
                            {"kind": "goal_met_diagnostic", **diag},
                        )
                    except OSError:
                        pass
            save_state(state, resolved_state_dir)
            _write_final_verdict(resolved_state_dir, payload)
            return payload

        # 1b. nothing_to_fix — every still-failing unit is a confirmed-fail we
        # could NOT reproduce. There is no defect to patch, so no wave can move
        # this. `unit_reproduced.get(u, True)` defaults to True so a unit with
        # no recorded flag (legacy artifact) can never trigger the stop — it is
        # conservative in the direction that preserves today's behaviour.
        if failing_after and unit_reproduced is not None and all(
            not unit_reproduced.get(u, True) for u in failing_after
        ):
            payload = {
                "stop": "nothing_to_fix",
                "waves": wave_no,
                "failing_units": failing_now,
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
                "failing_units": failing_now,
                "quarantined_units": sorted(quarantined),
                "spent_usd": spent,
                "projected_next_cost": projected,
                "budget_total_usd": budget_total_usd,
                **_unknown_fragment(state),
            }
            save_state(state, resolved_state_dir)
            _write_final_verdict(resolved_state_dir, payload)
            return payload

        # 3. diverged — UCCI divergence-kill (signature repeat or regressing).
        div = divergence(state, patience=divergence_patience, rdisc=_env_bool("MO_GOAL_RDISC", True))
        if div is not None:
            payload = {
                "stop": "diverged",
                "signature": div,
                "waves": wave_no,
                "failing_units": failing_now,
                "quarantined_units": sorted(quarantined),
                **_unknown_fragment(state),
            }
            save_state(state, resolved_state_dir)
            _write_final_verdict(resolved_state_dir, payload)
            return payload

        # 4. all_quarantined — every still-failing unit is GRAO-quarantined.
        if failing_after and all(u in quarantined for u in failing_after):
            payload = {
                "stop": "all_quarantined",
                "waves": wave_no,
                "failing_units": failing_now,
                "quarantined_units": sorted(quarantined),
            }
            save_state(state, resolved_state_dir)
            _write_final_verdict(resolved_state_dir, payload)
            return payload

        # No stop fired; persist state and continue.
        save_state(state, resolved_state_dir)

    # max_waves reached — soft "exhaustion" stop (safety case).
    final_failing = _last_measured_failing(state)
    payload = {
        "stop": "max_waves_reached",
        "waves": len(state["waves"]),
        "failing_units": final_failing,
        "quarantined_units": sorted(
            u for u, h in state.get("failed_fixes", {}).items()
            if len(h[-quarantine_patience:]) >= quarantine_patience
            and len(set(h[-quarantine_patience:])) == 1
        ),
        **_unknown_fragment(state),
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