"""bin/mini-ork-mcp-steering ``get_operator_steering`` — Python port.

Faithful re-implementation of the deterministic SQLite sub-pipeline inside
``bin/mini-ork-mcp-steering`` (the inner ``get_operator_steering`` function,
not the JSON-RPC transport, the TOOL_DEFS metadata, the ``_log`` helper
in its MCP-server capacity, or ``main()`` — those stay in the bin; this
module is the deterministic core other Python callers can import).

The bash function under parity test is ``operator_steering_fetch_for`` in
``lib/operator_steering.sh``. Both the bin's inner function and the bash
lib share the exact same SELECT / UPDATE / projection — parity is enforced
by ``tests/unit/test_mini_ork_mcp_steering_py.py`` (>=6 live-subprocess
cases that diff row contents against the bash output; floats 1e-6,
id/created_at/expires_at stripped).
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

__all__ = ["get_operator_steering"]


def _log(msg: str) -> None:
    """Mirror bin's stderr-only logger; stdout is the MCP protocol channel
    when invoked via the bin, and the parity test never triggers this branch
    (it always sets MINI_ORK_DB to a real file), but we keep it for verbatim
    parity with the bin's behavior on a missing DB."""
    print(f"[mini-ork-mcp-steering] {msg}", file=sys.stderr, flush=True)


def _resolve_db() -> str:
    """Three-tier precedence matching bin's ``_db_path``:

        ${MINI_ORK_DB:-${MINI_ORK_HOME:-$PWD/.mini-ork}/state.db}

    MINI_ORK_DB wins; else MINI_ORK_HOME/state.db; else cwd/.mini-ork/state.db."""
    explicit = os.environ.get("MINI_ORK_DB")
    if explicit:
        return explicit
    home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    return os.path.join(home, "state.db")


def _now_ms() -> int:
    return int(time.time() * 1000)


def get_operator_steering(run_id: str, role: str) -> list[dict]:
    """Fetch + consume unconsumed steering rows for (run_id, role).

    Verbatim port of ``bin/mini-ork-mcp-steering``'s inner function. Returns
    up to 10 rows ordered by severity tier DESC, confidence DESC,
    created_at DESC. Same SELECT/UPDATE/projection as
    ``lib/operator_steering.sh:operator_steering_fetch_for`` so parity tests
    can diff row-for-row.

    Missing DB → logs to stderr and returns ``[]`` (bin behavior; lib's bash
    is silent, but the plan specifies verbatim port of the bin).
    """
    db = _resolve_db()
    if not os.path.exists(db):
        _log(f"db not found: {db}")
        return []
    now_ms = _now_ms()
    con = sqlite3.connect(db, timeout=5.0)
    con.execute("PRAGMA busy_timeout = 5000")
    try:
        cur = con.execute(
            """SELECT id, run_id, role_target, severity, message, source,
                      confidence, created_at, expires_at
                 FROM operator_steering
                WHERE consumed_at IS NULL
                  AND expires_at > ?
                  AND (run_id = ? OR run_id IS NULL)
                  AND (role_target = ? OR role_target = 'any')
                ORDER BY
                  CASE severity WHEN 'critical' THEN 3 WHEN 'warn' THEN 2 ELSE 1 END DESC,
                  confidence DESC,
                  created_at DESC
                LIMIT 10""",
            (now_ms, run_id or "", role),
        )
        rows = cur.fetchall()
        out: list[dict] = []
        ids: list[int] = []
        for r in rows:
            ids.append(int(r[0]))
            out.append({
                "id": int(r[0]),
                "run_id": r[1],
                "role_target": r[2],
                "severity": r[3],
                "message": r[4],
                "source": r[5],
                "confidence": r[6],
                "created_at": int(r[7]),
                "expires_at": int(r[8]),
            })
        if ids:
            placeholders = ",".join("?" for _ in ids)
            con.execute(
                f"UPDATE operator_steering SET consumed_at = ? WHERE id IN ({placeholders})",
                [int(now_ms), *ids],
            )
            con.commit()
        return out
    finally:
        con.close()