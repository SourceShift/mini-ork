"""Mini-ork gate bootstrap — Python port of lib/gates_common.sh.

Faithful port of the bash ``mo_grounded_rejection`` helper required on every
gate fail/needs_revision verdict per HarnessBridge Technique 4
(arxiv:2606.12882) and the 2026-06-15 audit Theme C finding. The reflector
reads structured rows from ``grounded_rejections`` (per
``db/migrations/0037_grounded_rejection.sql``) instead of re-extracting tuples
from free-text rationale.

Public API mirrors ``lib/gates_common.sh`` exactly:
    tuple_json(concern, evidence, suggestion) -> str
    emit(gate, verdict, concern, evidence_summary, suggestion,
         evidence_trace_ids='[]', run_id=None) -> str | int

Return-value contract (matches bash):
    tuple_json → JSON string on success; raises ValueError on missing required
                 args (bash returns rc=2 in that case).
    emit       → hex id (str) on success; rc (int: 2/3/0) on failure.
                 rc=2 argument error, rc=3 JSON validation error, rc=0 success
                 OR table-absent no-op.

Strangler-fig note: this co-exists with lib/gates_common.sh; the bash file is
not modified by the port. The Python side uses sqlite3 ``?`` parameter binding
instead of bash's triple-quoted heredoc, so it's stricter against input but
the observable row content is identical to a successful bash emit.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timezone


_VALID_VERDICTS = frozenset({"fail", "needs_revision", "indeterminate"})


def _log(level: str, msg: str) -> None:
    """Mirror of bash ``_mo_gr_log``: structured JSON line to stderr."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sys.stderr.write(json.dumps({
        "level": level,
        "subsystem": "gates_common",
        "ts": ts,
        "msg": msg,
    }, ensure_ascii=False) + "\n")


def _resolve_db() -> str:
    """Mirror of bash ``_mo_gr_db``: env MINI_ORK_DB wins, else
    ${MINI_ORK_HOME:-pwd}/.mini-ork/state.db."""
    db = os.environ.get("MINI_ORK_DB")
    if db:
        return db
    home = os.environ.get("MINI_ORK_HOME") or os.getcwd()
    return f"{home}/.mini-ork/state.db"


def _table_exists(db: str) -> bool:
    try:
        con = sqlite3.connect(db)
        try:
            r = con.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='grounded_rejections' LIMIT 1"
            ).fetchone()
            return r is not None
        finally:
            con.close()
    except sqlite3.Error:
        return False


def _validate_verdict(v: str) -> bool:
    return v in _VALID_VERDICTS


def _validate_json_array(payload: str) -> bool:
    """Mirror of bash ``_mo_gr_validate_json_array``: empty → ok; otherwise
    must parse as a JSON array (top-level list)."""
    if not payload:
        return True
    try:
        v = json.loads(payload)
    except (ValueError, TypeError):
        return False
    return isinstance(v, list)


def tuple_json(concern: str, evidence: str, suggestion: str) -> str:
    """Mirror of ``mo_grounded_rejection_tuple_json``.

    Returns the canonical ``{concern, evidence, suggestion}`` JSON tuple
    (ensure_ascii=False, matching bash). Raises ValueError on missing
    required arg — bash returns rc=2 in that case; the test treats both as
    "rejection on missing input".
    """
    if not concern or not evidence or not suggestion:
        _log("error", "mo_grounded_rejection_tuple_json <concern> <evidence> <suggestion>")
        raise ValueError(
            "tuple_json requires non-empty concern, evidence, suggestion"
        )
    return json.dumps(
        {"concern": concern, "evidence": evidence, "suggestion": suggestion},
        ensure_ascii=False,
    )


def emit(gate: str, verdict: str, concern: str, evidence_summary: str,
         suggestion: str, evidence_trace_ids: str = "[]",
         run_id: str | None = None) -> str | int:
    """Mirror of ``mo_grounded_rejection``.

    Returns the hex id (str, 32 chars) on success; rc (int 2/3/0) on failure.
    When the ``grounded_rejections`` table is absent, behaves as a no-op and
    returns 0 (matches bash: warn-logged then rc=0).
    """
    if not gate or not verdict or not concern or not evidence_summary or not suggestion:
        _log(
            "error",
            "mo_grounded_rejection <gate> <verdict> <concern> "
            "<evidence_summary> <suggestion> [trace_ids_json] [run_id]",
        )
        return 2
    if not _validate_verdict(verdict):
        _log(
            "error",
            f"invalid verdict: {verdict} (must be fail|needs_revision|indeterminate)",
        )
        return 2
    if not _validate_json_array(evidence_trace_ids):
        _log("error", "evidence_trace_ids must be a JSON array")
        return 3
    db = _resolve_db()
    if not _table_exists(db):
        _log("warn", "grounded_rejections table absent; emit is a no-op. Run migrations.")
        return 0

    new_id = secrets.token_hex(16)
    con = sqlite3.connect(db)
    try:
        con.execute(
            """INSERT INTO grounded_rejections
                 (id, gate_name, verdict, concern, evidence_trace_ids,
                  evidence_summary, suggestion, run_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id,
                gate,
                verdict,
                concern,
                evidence_trace_ids,
                evidence_summary,
                suggestion,
                run_id if run_id else None,
            ),
        )
        con.commit()
    finally:
        con.close()
    _log(
        "info",
        f"emitted grounded_rejection id={new_id} gate={gate} "
        f"verdict={verdict} run={run_id or ''}",
    )
    return new_id
