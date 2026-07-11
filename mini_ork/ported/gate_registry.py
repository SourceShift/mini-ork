"""Faithful Python port of ``lib/gate_registry.sh``.

Strangler-fig co-existence: ``lib/gate_registry.sh`` stays the canonical
implementation. This module mirrors its 4 public functions
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
* **liveness_gate and external-callable types subprocess bash.**
  ``liveness_gate`` reads verdict from ``mo_check_liveness_breaker``
  in ``lib/circuit_breaker.sh`` (bash-only state machine). The four
  external-callable types dispatch ``<condition> <context>`` with rc
  0=pass, 2=defer, else=fail — a function-name protocol that lives in
  the caller shell. Reimplementing either in Python would be a
  behavioral reinvention; the port keeps them as a thin subprocess
  shim so parity holds at the ``bash`` boundary, not at some
  Python-imagined equivalent.
* **Bash ``null`` → Python ``None``** for missing/inactive rows.

Public API (mirrors bash function names exactly)::

    from mini_ork.ported.gate_registry import (
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
import shlex
import sqlite3
import subprocess
import time
import uuid
from typing import Any, Optional

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
    if gate_type not in _VALID_GATE_TYPES:
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


def _bash_subprocess(
    script: str, env_extra: dict[str, str], timeout: int = 30
) -> tuple[str, str, int]:
    """Run a bash -c fragment with ``env_extra`` merged onto os.environ."""
    env = {**os.environ, **env_extra}
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    return proc.stdout, proc.stderr, proc.returncode


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
    """Subprocess bash to call mo_check_liveness_breaker in lib/circuit_breaker.sh."""
    try:
        ctx = json.loads(context_json)
        run_id = ctx.get("run_id", "")
    except Exception:
        return "defer"
    if not run_id:
        return "defer"

    root = mini_ork_root or os.environ.get("MINI_ORK_ROOT", "")
    cb_path = os.path.join(root, "lib", "circuit_breaker.sh") if root else ""
    if not cb_path or not os.path.isfile(cb_path):
        return "defer"

    src = (
        f'source "{cb_path}" 2>/dev/null || true; '
        f'if declare -f mo_check_liveness_breaker >/dev/null 2>&1; then '
        f'  mo_check_liveness_breaker "{run_id}" 2>/dev/null || true; '
        f'else echo ""; fi'
    )
    stdout, _, _ = _bash_subprocess(
        src, {"MINI_ORK_DB": db_path, "MINI_ORK_ROOT": root}, timeout=10
    )
    out = (stdout or "").strip()
    if not out:
        return "defer"
    try:
        verdict = json.loads(out).get("verdict", "PROCEED")
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

    Mirrors bash's ``declare -f`` + ``[[ -x ]]`` heuristic. We can't
    introspect the caller's bash functions from Python, so we check
    whether ``condition`` resolves to an executable path; otherwise
    fall through to ``defer`` (matches bash's stderr-then-defer).
    """
    if not condition:
        return "defer"
    if os.path.isfile(condition) and os.access(condition, os.X_OK):
        cmd = [condition, context_json]
    else:
        # Try as a function name by shelling through bash with an
        # empty env-stripped prelude: only succeeds if the test
        # process sourced lib/gate_registry.sh. The parity test
        # intentionally sources a helper to exercise this path; if
        # the function isn't found, bash exits 127 → fall to fail.
        cmd = ["bash", "-c", f'{condition} "$1"', "bash", context_json]
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


def gate_evaluate(db_path: str, gate_id: str, context_json: str) -> str:
    """Evaluate a single gate. Returns ``'pass' | 'fail' | 'defer'``."""
    ensure_table(db_path)
    row = _select_gate(db_path, gate_id)
    if row is None:
        return "fail"

    gtype = row["gate_type"]
    condition = row["condition"]

    if gtype == "budget_gate":
        return _evaluate_budget(context_json, condition)
    if gtype == "human_gate":
        return "defer"
    if gtype == "scope_gate":
        return _evaluate_scope(context_json, condition)
    if gtype == "liveness_gate":
        return _evaluate_liveness(context_json, db_path, None)
    if gtype in ("deployment_gate", "reviewer_gate", "deterministic_verifier", "custom"):
        return _evaluate_external(condition, context_json, db_path)
    return "defer"


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
        cond = g["condition"]
        safety = bool(g["safety"])

        # Mirror the bash inline branch table in gate_run_all.
        if gtype == "budget_gate":
            verdict = _evaluate_budget(context_json, cond)
        elif gtype == "human_gate":
            verdict = "defer"
        elif gtype == "scope_gate":
            verdict = _evaluate_scope(context_json, cond)
        else:
            # Bash fall-through: subprocess gate_evaluate from the
            # sourced shell, capturing stdout. The port reproduces
            # this path exactly because the verdict is determined by
            # either mo_check_liveness_breaker (liveness_gate) or the
            # function-name rc protocol (external types) — both of
            # which are bash-side state.
            root = mini_ork_root or os.environ.get("MINI_ORK_ROOT", "")
            cb_src = (
                f'source "{root}/lib/gate_registry.sh" 2>/dev/null; '
                f'gate_evaluate "{gid}" {shlex.quote(context_json)}'
            )
            try:
                stdout, _, _ = _bash_subprocess(
                    cb_src,
                    {"MINI_ORK_DB": db_path, "MINI_ORK_ROOT": root},
                    timeout=30,
                )
                v = stdout.strip()
                verdict = v if v in ("pass", "fail", "defer") else "defer"
            except Exception:
                verdict = "defer"

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