"""Route reflection gradients to the layer that can actually fix them.

A diagnosed failure carries a typed :data:`mini_ork.recovery.diagnose.LAYERS`
layer. Every gradient target family this module understands (``workflow.*``,
``agent.*``, ``verifier.*``, ``harness.*``, ``prompts/`` and their
``cross_class:`` variants) edits the **recipe harness**. When the diagnosis puts
the failure in a different layer — a bad kickoff, a dead lane, a missing
credential, an engine bug — a harness directive cannot fix it, so the gradient is
refused and the place the fix belongs (``routes_to``) is recorded instead.

The gate is shadow-first: an unset or unknown ``MO_LAYER_ROUTING`` records what
it *would* refuse without refusing, so an operator can review the refusal log
before switching the rule on with ``MO_LAYER_ROUTING=on``.

All functions are module-level and fail soft: a missing table, a missing file or
an unreadable DB admits the gradient rather than stopping reflection.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from mini_ork.recovery.diagnose import DIAGNOSIS_FILE, LAYER_CHANGE, LAYER_UNCLASSIFIED
from mini_ork.run_status import UNSUCCESSFUL

EDIT_LAYER_HARNESS = "harness"
HARNESS_TARGET_PREFIXES = ("workflow.", "agent.", "verifier.", "harness.", "prompts/")
CROSS_CLASS_PREFIX = "cross_class:"
HARNESS_FIXABLE_LAYERS = frozenset({LAYER_CHANGE})
PASS_THROUGH_LAYERS = frozenset({"", LAYER_UNCLASSIFIED})
REFUSALS_FILE = "layer-refusals.jsonl"


def mode() -> str:
    """Return the configured gate mode: ``off``, ``on`` or ``shadow``.

    An unset or unknown value is ``shadow``: the rule records what it would
    refuse (106 of 319 diagnosed-failure gradients on 2026-10-27) without
    refusing, until an operator reviews the refusal log and sets
    ``MO_LAYER_ROUTING=on``.
    """
    v = os.environ.get("MO_LAYER_ROUTING", "").strip().lower()
    return "off" if v == "off" else "on" if v == "on" else "shadow"


def edit_layer(target: str) -> str:
    """Return ``harness`` when ``target`` edits the recipe harness, else ``""``.

    One leading ``cross_class:`` prefix is stripped before the match.
    """
    rest = target[len(CROSS_CLASS_PREFIX):] if target.startswith(CROSS_CLASS_PREFIX) else target
    return EDIT_LAYER_HARNESS if rest.startswith(HARNESS_TARGET_PREFIXES) else ""


def route(diagnosis: Mapping[str, Any] | None, target: str) -> dict:
    """Decide whether a gradient for ``target`` may be authored.

    Returns ``{"admit", "edit_layer", "diagnosed_layer", "reroute", "reason"}``.
    The first matching rule wins; a layer outside ``diagnose.LAYERS`` fails
    closed unless it is empty or ``unclassified``.
    """
    edit = edit_layer(target)
    if isinstance(diagnosis, Mapping):
        layer = str(diagnosis.get("layer") or "")
    else:
        layer = ""
    verdict = {
        "admit": True,
        "edit_layer": edit,
        "diagnosed_layer": layer,
        "reroute": "",
        "reason": "",
    }
    if edit == "":
        verdict["reason"] = "untyped target"
    elif not isinstance(diagnosis, Mapping):
        verdict["reason"] = "no diagnosis"
    elif layer in PASS_THROUGH_LAYERS:
        verdict["reason"] = "unclassified diagnosis"
    elif layer in HARNESS_FIXABLE_LAYERS:
        verdict["reason"] = "layer match"
    else:
        reroute = str(diagnosis.get("routes_to") or "")
        verdict["admit"] = False
        verdict["reroute"] = reroute
        verdict["reason"] = (
            f"layer mismatch: a {edit} directive cannot fix a {layer}-layer failure "
            f"(routes_to={reroute or 'none'})"
        )
    return verdict


def trace_run_id(db: str, trace_id: str) -> str:
    """Return the ``run_id`` recorded for ``trace_id``, or ``""``."""
    try:
        con = sqlite3.connect(db)
        try:
            row = con.execute(
                "SELECT run_id FROM execution_traces WHERE trace_id=?", (trace_id,)
            ).fetchone()
        finally:
            con.close()
    except Exception:  # noqa: BLE001 - any sqlite error admits the gradient
        return ""
    if not row or row[0] is None:
        return ""
    return str(row[0])


def run_diagnosis(home: str, run_id: str, db: str) -> dict | None:
    """Return the diagnosis dict for an unsuccessful run, or ``None``.

    A published, superseded or still-live run's diagnosis is stale (or absent),
    so only statuses in :data:`mini_ork.run_status.UNSUCCESSFUL` are considered.
    """
    if not run_id:
        return None
    try:
        con = sqlite3.connect(db)
        try:
            row = con.execute(
                "SELECT status FROM task_runs WHERE id=?", (run_id,)
            ).fetchone()
        finally:
            con.close()
    except Exception:  # noqa: BLE001 - any sqlite error admits the gradient
        return None
    if not row or row[0] not in UNSUCCESSFUL:
        return None
    try:
        data = json.loads((Path(home) / "runs" / run_id / DIAGNOSIS_FILE).read_text())
    except Exception:  # noqa: BLE001 - missing/unreadable/non-JSON admits
        return None
    return data if isinstance(data, dict) else None


def check_gradient(gradient: Mapping[str, Any], *, trace_id: str, db: str, home: str) -> dict:
    """Resolve a gradient's run diagnosis and route it by layer."""
    target = str(gradient.get("target") or "")
    run_id = trace_run_id(db, trace_id)
    dx = run_diagnosis(home, run_id, db) if run_id else None
    verdict = route(dx, target)
    verdict["trace_id"] = trace_id
    verdict["run_id"] = run_id
    verdict["target"] = target
    return verdict


def record_refusal(home: str, run_id: str, refusal: Mapping[str, Any]) -> bool:
    """Append one sorted-key JSON line to the run's refusal log.

    Returns ``False`` when there is no run directory to write into or the write
    fails; the caller must never fail because a refusal could not be logged.
    """
    if not run_id:
        return False
    run_dir = Path(home) / "runs" / run_id
    if not run_dir.is_dir():
        return False
    try:
        with open(run_dir / REFUSALS_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(refusal), sort_keys=True) + "\n")
    except (OSError, TypeError, ValueError):
        return False
    return True
