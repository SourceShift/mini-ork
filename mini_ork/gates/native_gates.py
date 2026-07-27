"""Native evaluators for the 5 oracle gates — replaces the gates/*.sh shims.

WS4 (bash-removal): the production gate path
(``publisher_node`` → ``gate_bootstrap.bootstrap_oracle_gates`` →
``gate_registry.gate_run_all`` → ``gate_evaluate``) previously executed the
registered gate *condition* as a bash script via subprocess. The 5 oracle
gates seeded by ``gate_bootstrap`` pointed at
``gates/{coalition,panel-health,synthesis-promote,stability,liveness}.sh`` —
thin shims that sourced a bash lib, called one function, and mapped the
JSON verdict onto the rc contract (0=pass, 1=fail, 2=defer).

This module maps each shim onto its staged Python port, preserving the
shim's exact verdict contract:

  ==================  ====================================  ============================
  shim (condition)    staged port                           verdict mapping
  ==================  ====================================  ============================
  coalition.sh        gates/coalition_gate.py               panel_diverse→pass,
                      (+ observability/topology_metrics     COALITION_ABORT→fail,
                       .measure_rho for ρ)                   else→defer
  liveness.sh         recovery/circuit_breaker.py           PROCEED|PROBE→pass,
                                                            LIVENESS_TRIP→fail,
                                                            else→defer
  panel-health.sh     gates/cw_por.py                       panel_healthy|indeterminate
                                                            →pass, authority_capture_
                                                            suspected→fail, else→defer
  stability.sh        gates/adaptive_stability.py           CONTINUE→pass, HALT→fail,
                                                            else→defer
  synthesis-promote.sh gates/promotion_gate.py              rc 0→pass, 1→fail, 2→defer
  ==================  ====================================  ============================

Condition resolution (``resolve_native_evaluator``) recognizes BOTH:

  * the new sentinel conditions seeded by ``gate_bootstrap`` —
    ``native:<name>`` (e.g. ``native:coalition``), and
  * the legacy script-path conditions already present in live DBs —
    any path whose basename is one of the 5 known shim filenames
    (e.g. ``/repo/gates/coalition.sh``). These rows keep working with
    zero migration, but no longer execute bash.

Unrecognized conditions fall through to the legacy executable contract in
``gate_registry._evaluate_external`` (script exists + executable → run it;
else defer), so user-registered custom script gates are unaffected.

Evaluator signature matches ``gate_registry.GateEvaluator``:
``(condition, context_json, db_path, mini_ork_root) -> 'pass'|'fail'|'defer'``.
"""
from __future__ import annotations

import json
import os
from typing import Callable, Optional

__all__ = [
    "NATIVE_GATE_EVALUATORS",
    "NATIVE_SENTINEL_PREFIX",
    "register_native_gate",
    "resolve_native_evaluator",
    "native_condition",
]

NativeGateEvaluator = Callable[[str, str, str, Optional[str]], str]

#: Prefix for DB ``condition`` strings that name a native evaluator instead
#: of an executable path (seeded by ``gate_bootstrap``).
NATIVE_SENTINEL_PREFIX = "native:"

#: Basenames of the legacy bash shims → canonical native gate name. Rows in
#: live DBs that still point at these script paths resolve to the native
#: evaluator (the script is NEVER executed for a recognized oracle gate).
_SCRIPT_BASENAME_TO_NAME: dict[str, str] = {
    "coalition.sh": "coalition",
    "panel-health.sh": "panel-health",
    "synthesis-promote.sh": "synthesis-promote",
    "stability.sh": "stability",
    "liveness.sh": "liveness",
}


def native_condition(name: str) -> str:
    """Return the sentinel condition string for a native gate name."""
    return f"{NATIVE_SENTINEL_PREFIX}{name}"


def _parse_context(context_json: str) -> Optional[dict]:
    """Parse the context JSON; ``None`` mirrors the shims' empty/malformed
    ``jq`` extraction (which defers)."""
    try:
        ctx = json.loads(context_json)
    except Exception:
        return None
    return ctx if isinstance(ctx, dict) else None


# ── coalition (gates/coalition.sh → gates/coalition_gate.py) ─────────────────


def _eval_coalition(
    condition: str, context_json: str, db_path: str, mini_ork_root: Optional[str]
) -> str:
    del condition
    ctx = _parse_context(context_json)
    if ctx is None:
        return "defer"
    panel_run_id = ctx.get("panel_run_id") or ""
    recipe = ctx.get("recipe") or ""
    if not panel_run_id or not recipe:
        return "defer"
    try:
        from mini_ork.gates import coalition_gate
        from mini_ork.observability import topology_metrics

        # bash measures ρ via lib/topology_metrics.sh::measure_rho; the
        # native port lives in observability/topology_metrics.py.
        rho = topology_metrics.measure_rho(db_path, panel_run_id)
        root = mini_ork_root or os.environ.get("MINI_ORK_ROOT", "")
        agents_yaml = (
            os.path.join(root, "config", "agents.yaml") if root else None
        )
        payload, _rc = coalition_gate.check_panel_coalition(
            panel_run_id, recipe, rho=rho, db=db_path, agents_yaml=agents_yaml
        )
        verdict = payload.get("verdict", "indeterminate")
    except Exception:
        return "defer"
    if verdict == "panel_diverse":
        return "pass"
    if verdict == "COALITION_ABORT":
        return "fail"
    return "defer"


# ── liveness (gates/liveness.sh → recovery/circuit_breaker.py) ───────────────


def _eval_oracle_liveness(
    condition: str, context_json: str, db_path: str, mini_ork_root: Optional[str]
) -> str:
    del condition, mini_ork_root
    ctx = _parse_context(context_json)
    if ctx is None:
        return "defer"
    # The shim accepts either `run_id` (preferred) or `panel_run_id`
    # (the central wire-up key shared across oracle gates).
    run_id = ctx.get("run_id") or ctx.get("panel_run_id") or ""
    if not run_id:
        return "defer"
    try:
        from mini_ork.recovery import circuit_breaker

        # Forward the MO_CB_* env knobs exactly as the bash lib reads them
        # at function entry (the port takes them as explicit kwargs).
        payload, _rc = circuit_breaker.check_liveness_breaker(
            run_id,
            db=db_path,
            artifact_window=int(os.environ.get("MO_CB_ARTIFACT_WINDOW", "3")),
            verdict_window=int(os.environ.get("MO_CB_VERDICT_WINDOW", "3")),
            cost_threshold=float(os.environ.get("MO_CB_COST_THRESHOLD", "1.00")),
            policy=os.environ.get("MO_CB_POLICY", "majority"),
            cooldown_s=int(os.environ.get("MO_CB_COOLDOWN_S", "1800")),
        )
        verdict = payload.get("verdict", "PROCEED")
    except Exception:
        return "defer"
    if verdict in ("PROCEED", "PROBE"):
        return "pass"
    if verdict == "LIVENESS_TRIP":
        return "fail"
    return "defer"


# ── panel-health (gates/panel-health.sh → gates/cw_por.py) ───────────────────


def _eval_panel_health(
    condition: str, context_json: str, db_path: str, mini_ork_root: Optional[str]
) -> str:
    del condition, db_path, mini_ork_root
    ctx = _parse_context(context_json)
    if ctx is None:
        return "defer"
    verdict_file = ctx.get("verdict_file") or ""
    if not verdict_file or not os.path.isfile(verdict_file):
        return "defer"
    try:
        from mini_ork.gates import cw_por

        # compute_cw_por raises FileNotFoundError/ValueError on the same
        # inputs the bash lib answers rc=2 for → shim defers.
        _rc, payload = cw_por.compute_cw_por(verdict_file)
        verdict = payload.get("verdict", "indeterminate")
    except Exception:
        return "defer"
    if verdict in ("panel_healthy", "indeterminate"):
        return "pass"
    if verdict == "authority_capture_suspected":
        return "fail"
    return "defer"


# ── stability (gates/stability.sh → gates/adaptive_stability.py) ─────────────


def _eval_stability(
    condition: str, context_json: str, db_path: str, mini_ork_root: Optional[str]
) -> str:
    del condition, mini_ork_root
    ctx = _parse_context(context_json)
    if ctx is None:
        return "defer"
    panel_run_id = ctx.get("panel_run_id") or ""
    if not panel_run_id:
        return "defer"
    current_round = ctx.get("current_round", 1)
    try:
        current_round = int(current_round)
    except (TypeError, ValueError):
        current_round = 1
    try:
        from mini_ork.gates import adaptive_stability

        # Forward the MO_PANEL_* env knobs exactly as the bash lib reads
        # them at function entry (the port takes them as explicit kwargs).
        payload = adaptive_stability.check_panel_stability(
            panel_run_id,
            current_round,
            db=db_path,
            threshold=float(os.environ.get("MO_PANEL_STABILITY_THRESHOLD", "0.10")),
            min_rounds=int(os.environ.get("MO_PANEL_MIN_ROUNDS", "2")),
            max_rounds=int(os.environ.get("MO_PANEL_MAX_ROUNDS", "5")),
        )
        recommendation = payload.get("recommendation", "CONTINUE")
    except Exception:
        return "defer"
    if recommendation == "CONTINUE":
        return "pass"
    if recommendation == "HALT":
        return "fail"
    return "defer"


# ── synthesis-promote (gates/synthesis-promote.sh → gates/promotion_gate.py) ──


def _eval_synthesis_promote(
    condition: str, context_json: str, db_path: str, mini_ork_root: Optional[str]
) -> str:
    del condition, db_path
    ctx = _parse_context(context_json)
    if ctx is None:
        return "defer"
    verdict_file = ctx.get("verdict_file") or ""
    task_class = ctx.get("task_class") or ""
    if not verdict_file or not os.path.isfile(verdict_file) or not task_class:
        return "defer"
    try:
        from mini_ork.gates import promotion_gate

        _payload, rc = promotion_gate.mo_promote_synthesis_gate(
            verdict_file, task_class, mini_ork_root=mini_ork_root
        )
    except Exception:
        return "defer"
    if rc == 0:
        return "pass"
    if rc == 1:
        return "fail"
    return "defer"


# ── registry ──────────────────────────────────────────────────────────────────

NATIVE_GATE_EVALUATORS: dict[str, NativeGateEvaluator] = {
    "coalition": _eval_coalition,
    "liveness": _eval_oracle_liveness,
    "panel-health": _eval_panel_health,
    "stability": _eval_stability,
    "synthesis-promote": _eval_synthesis_promote,
}


def register_native_gate(name: str, evaluator: NativeGateEvaluator) -> None:
    """Register (or replace) the native evaluator for an oracle gate name.

    OCP extension path: a new native gate becomes evaluatable by adding a
    ``native:<name>`` condition here — no edit to ``gate_registry``.
    """
    NATIVE_GATE_EVALUATORS[name] = evaluator


def resolve_native_evaluator(condition: str) -> Optional[NativeGateEvaluator]:
    """Map a gate ``condition`` string to a native evaluator, or ``None``.

    Recognized forms:
      * ``native:<name>``                — the bootstrap sentinel
      * ``<anything>/<name>.sh``         — legacy script path (basename of
                                           one of the 5 oracle shims)
    Anything else returns ``None`` and the caller falls back to the legacy
    executable contract.
    """
    if not condition:
        return None
    if condition.startswith(NATIVE_SENTINEL_PREFIX):
        name = condition[len(NATIVE_SENTINEL_PREFIX):]
        return NATIVE_GATE_EVALUATORS.get(name)
    name = _SCRIPT_BASENAME_TO_NAME.get(os.path.basename(condition))
    if name is not None:
        return NATIVE_GATE_EVALUATORS.get(name)
    return None
