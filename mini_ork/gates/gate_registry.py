"""Python-owned gate registry.

This module implements the 4 public functions
(``gate_register``, ``gate_evaluate``, ``gate_list``, ``gate_run_all``)
and the 7 valid gate types
(``deterministic_verifier``, ``reviewer_gate``, ``human_gate``,
``budget_gate``, ``scope_gate``, ``deployment_gate``, ``liveness_gate``,
``custom``) verbatim.

Design notes
------------

* **Lazy CREATE TABLE.** The bash side uses ``_gate_ensure_table``
  guarded by a per-process ``_MO_GATE_SCHEMA_INIT`` flag. The Python
  port has no per-process state and is naturally idempotent via
  ``CREATE TABLE IF NOT EXISTS`` (PRAGMA busy_timeout=5000 to match
  bash heredoc).
* **Gate-id parity.** ``gate-<gtype[:6]>-<uuid4().hex[:8]>`` is
  mirrored exactly so a side-by-side row diff across both
  implementations matches when the random suffix is excluded.
* **Native liveness, explicit executable gates.** ``liveness_gate`` uses the
  native circuit-breaker port. External-callable gate types may execute an
  explicit executable contract with rc 0=pass, 2=defer, else=fail. Shell
  function names are intentionally not resolved through an implicit Bash
  process; unresolved conditions defer.
* **Bash ``null`` → Python ``None``** for missing/inactive rows.

Public API (mirrors bash function names exactly)::

    from mini_ork.gates.gate_registry import (
        ensure_table,            # (db_path) -> None
        gate_register,           # (db_path, gate_type, condition, task_class_filter='', safety=False) -> gate_id
        gate_evaluate,           # (db_path, gate_id, context_json) -> 'pass'|'fail'|'defer'
        gate_list,               # (db_path, task_class=None) -> list[dict]
        gate_run_all,            # (db_path, task_class, context_json, mini_ork_root=None) -> dict
    )

Parity is enforced by ``tests/unit/test_gate_registry_py.py`` (>=6
live-subprocess cases against the bash function).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
import uuid
from typing import Any, Callable, Optional

__all__ = [
    "ensure_table",
    "gate_register",
    "gate_evaluate",
    "gate_list",
    "gate_run_all",
    "_VALID_GATE_TYPES",
]

_VALID_GATE_TYPES: tuple[str, ...] = (
    "deterministic_verifier",
    "reviewer_gate",
    "human_gate",
    "budget_gate",
    "scope_gate",
    "deployment_gate",
    "liveness_gate",
    "custom",
)


# ─────────────────────────────────────────────────────────────────────────────
# Schema — verbatim echo of the bash DDL in _gate_ensure_table.
# ─────────────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS gate_registry (
    gate_id             TEXT PRIMARY KEY,
    gate_type           TEXT NOT NULL,
    condition           TEXT NOT NULL,
    task_class_filter   TEXT,
    safety              INTEGER NOT NULL DEFAULT 0,
    active              INTEGER NOT NULL DEFAULT 1,
    registered_at       INTEGER NOT NULL
)
"""


def ensure_table(db_path: str) -> None:
    """Idempotent CREATE TABLE for ``gate_registry``.

    Mirrors bash ``_gate_ensure_table`` minus the per-process
    ``_MO_GATE_SCHEMA_INIT`` guard (CREATE TABLE IF NOT EXISTS is
    naturally idempotent across processes).
    """
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        con.execute(_DDL)
        con.commit()
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
# gate_register
# ─────────────────────────────────────────────────────────────────────────────


def gate_register(
    db_path: str,
    gate_type: str,
    condition: str,
    task_class_filter: str = "",
    safety: bool = False,
) -> str:
    """Register a gate. Returns the new ``gate_id``.

    ``condition`` is either a bash function name or a path to a script
    that exits 0=pass, 1=fail, 2=defer (matches bash semantics).

    Raises ``ValueError`` on unknown ``gate_type`` (mirrors bash's
    ``rc=1`` + stderr). Returns ``""`` (empty string) on the rare
    uuid collision path via ``ON CONFLICT(gate_id) DO NOTHING`` —
    bash prints nothing on collision so the port matches.
    """
    # A type is valid when it is one of the historical built-ins OR has a
    # registered evaluator (the OCP extension path: register_gate_evaluator
    # makes a brand-new type registrable + evaluatable without editing this
    # module). GATE_EVALUATORS is defined below; resolved at call time.
    if gate_type not in _VALID_GATE_TYPES and gate_type not in GATE_EVALUATORS:
        raise ValueError(
            f"gate_register: unknown gate_type '{gate_type}'. "
            f"Valid: {' '.join(_VALID_GATE_TYPES)}"
        )

    ensure_table(db_path)

    gid = f"gate-{gate_type[:6]}-{uuid.uuid4().hex[:8]}"
    tcf = task_class_filter if task_class_filter else None
    now = int(time.time())

    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        cur = con.execute(
            """
            INSERT INTO gate_registry
                (gate_id, gate_type, condition, task_class_filter, safety, active, registered_at)
            VALUES (?,?,?,?,?,1,?)
            ON CONFLICT(gate_id) DO NOTHING
            """,
            (gid, gate_type, condition, tcf, int(bool(safety)), now),
        )
        con.commit()
        if cur.rowcount == 0:
            return ""
        return gid
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
# gate_evaluate
# ─────────────────────────────────────────────────────────────────────────────


def _select_gate(db_path: str, gate_id: str) -> Optional[dict[str, Any]]:
    con = sqlite3.connect(db_path)
    try:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM gate_registry WHERE gate_id=? AND active=1",
            (gate_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def _evaluate_budget(context_json: str, condition: str) -> str:
    try:
        ctx = json.loads(context_json)
        limit = float(condition)
        cost = float(ctx.get("cost_usd", 0.0))
        return "pass" if cost <= limit else "fail"
    except Exception:
        return "defer"


def _evaluate_scope(context_json: str, condition: str) -> str:
    try:
        ctx = json.loads(context_json)
        allowed = json.loads(condition)
        task_cls = ctx.get("task_class", "")
        return "pass" if task_cls in allowed else "fail"
    except Exception:
        return "defer"


def _evaluate_liveness(
    context_json: str, db_path: str, mini_ork_root: Optional[str]
) -> str:
    """Evaluate liveness through the native circuit-breaker port."""
    del mini_ork_root  # retained in the public signature for caller compatibility
    try:
        ctx = json.loads(context_json)
        run_id = ctx.get("run_id", "")
    except Exception:
        return "defer"
    if not run_id:
        return "defer"

    try:
        from mini_ork.recovery import circuit_breaker
        payload, _rc = circuit_breaker.check_liveness_breaker(run_id, db=db_path)
        verdict = payload.get("verdict", "PROCEED")
    except Exception:
        return "defer"
    if verdict == "LIVENESS_TRIP":
        return "fail"
    if verdict in ("PROCEED", "PROBE"):
        return "pass"
    return "defer"


def _evaluate_external(
    condition: str, context_json: str, db_path: str
) -> str:
    """dispatch <condition> <context_json>; rc 0=pass, 2=defer, else fail.

    Only an explicit executable is a supported external gate contract.
    Missing paths and former shell-function names defer.
    """
    if not condition:
        return "defer"
    if not (os.path.isfile(condition) and os.access(condition, os.X_OK)):
        return "defer"
    cmd = [condition, context_json]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "MINI_ORK_DB": db_path},
        )
    except Exception:
        return "defer"
    rc = proc.returncode
    if rc == 0:
        return "pass"
    if rc == 2:
        return "defer"
    return "fail"


# ── Gate-type evaluator registry (OCP) ──────────────────────────────────────
# Evaluator signature: (condition, context_json, db_path, mini_ork_root)
#   -> 'pass' | 'fail' | 'defer'.
# Registering a gate of a NEW type in the DB now works: pair the gate_register
# call with register_gate_evaluator() instead of editing gate_evaluate.

GateEvaluator = Callable[[str, str, str, Optional[str]], str]


def _eval_budget(condition: str, context_json: str, db_path: str,
                 mini_ork_root: Optional[str]) -> str:
    del db_path, mini_ork_root
    return _evaluate_budget(context_json, condition)


def _eval_human(condition: str, context_json: str, db_path: str,
                mini_ork_root: Optional[str]) -> str:
    del condition, context_json, db_path, mini_ork_root
    return "defer"


def _eval_scope(condition: str, context_json: str, db_path: str,
                mini_ork_root: Optional[str]) -> str:
    del db_path, mini_ork_root
    return _evaluate_scope(context_json, condition)


def _eval_liveness(condition: str, context_json: str, db_path: str,
                   mini_ork_root: Optional[str]) -> str:
    del condition
    return _evaluate_liveness(context_json, db_path, mini_ork_root)


def _eval_external(condition: str, context_json: str, db_path: str,
                   mini_ork_root: Optional[str]) -> str:
    del mini_ork_root
    return _evaluate_external(condition, context_json, db_path)


GATE_EVALUATORS: dict[str, GateEvaluator] = {
    "budget_gate": _eval_budget,
    "human_gate": _eval_human,
    "scope_gate": _eval_scope,
    "liveness_gate": _eval_liveness,
    "deployment_gate": _eval_external,
    "reviewer_gate": _eval_external,
    "deterministic_verifier": _eval_external,
    "custom": _eval_external,
}


def register_gate_evaluator(gate_type: str, evaluator: GateEvaluator) -> None:
    """Register (or replace) the evaluator for a gate type. Unregistered types
    keep the historical fail-safe: ``defer``."""
    GATE_EVALUATORS[gate_type] = evaluator


def gate_evaluate(
    db_path: str,
    gate_id: str,
    context_json: str,
    mini_ork_root: Optional[str] = None,
) -> str:
    """Evaluate a single gate. Returns ``'pass' | 'fail' | 'defer'``."""
    ensure_table(db_path)
    row = _select_gate(db_path, gate_id)
    if row is None:
        return "fail"

    evaluator = GATE_EVALUATORS.get(row["gate_type"])
    if evaluator is None:
        return "defer"
    return evaluator(row["condition"], context_json, db_path, mini_ork_root)


# ─────────────────────────────────────────────────────────────────────────────
# gate_list
# ─────────────────────────────────────────────────────────────────────────────


def gate_list(db_path: str, task_class: Optional[str] = None) -> list[dict[str, Any]]:
    """List active gates, optionally filtered by ``task_class``."""
    ensure_table(db_path)
    con = sqlite3.connect(db_path)
    try:
        con.row_factory = sqlite3.Row
        if task_class:
            rows = con.execute(
                """
                SELECT * FROM gate_registry
                WHERE active=1
                  AND (task_class_filter IS NULL OR task_class_filter=?)
                ORDER BY gate_type
                """,
                (task_class,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM gate_registry WHERE active=1 ORDER BY gate_type"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
# gate_run_all
# ─────────────────────────────────────────────────────────────────────────────


def gate_run_all(
    db_path: str,
    task_class: str,
    context_json: str,
    mini_ork_root: Optional[str] = None,
) -> dict[str, Any]:
    """Evaluate every applicable gate for ``task_class`` against ``context_json``.

    Returns a summary dict mirroring bash's printed JSON shape::

        {
          "task_class":       <str>,
          "all_pass":         <bool>,
          "any_defer":        <bool>,
          "safety_violation": <bool>,
          "gate_count":       <int>,
          "gates": [
            {"gate_id": <str>, "gate_type": <str>,
             "safety": <bool>, "verdict": "pass|fail|defer"},
            ...
          ],
        }
    """
    ensure_table(db_path)
    gates = gate_list(db_path, task_class=task_class)

    results: list[dict[str, Any]] = []
    all_pass = True
    any_defer = False
    safety_fail = False

    for g in gates:
        gid = g["gate_id"]
        gtype = g["gate_type"]
        safety = bool(g["safety"])

        verdict = gate_evaluate(
            db_path,
            gid,
            context_json,
            mini_ork_root=mini_ork_root,
        )

        if verdict != "pass":
            all_pass = False
        if verdict == "defer":
            any_defer = True
        if verdict == "fail" and safety:
            safety_fail = True

        results.append(
            {
                "gate_id": gid,
                "gate_type": gtype,
                "safety": safety,
                "verdict": verdict,
            }
        )

    try:
        task_class_ctx = json.loads(context_json).get("task_class", "") if context_json else ""
    except Exception:
        task_class_ctx = ""

    return {
        "task_class": task_class_ctx,
        "all_pass": all_pass,
        "any_defer": any_defer,
        "safety_violation": safety_fail,
        "gate_count": len(results),
        "gates": results,
    }
