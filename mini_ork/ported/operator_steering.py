"""Operator-steering emit + consume — Python port of lib/operator_steering.sh.

Faithful port of the deterministic SQLite sub-pipelines that
``lib/operator_steering.sh`` shells out to via inline ``python3`` heredocs.
The bash function definitions stay in place (strangler-fig co-existence);
this module gives Python callers an in-process surface and gives
``tests/unit/test_operator_steering_py.py`` a stable target to byte-diff
against the live bash subprocess.

Co-existence model (strangler-fig): bash ``lib/operator_steering.sh`` is the
authoritative source. This module mirrors its sub-pipelines exactly. Parity
is enforced by ``tests/unit/test_operator_steering_py.py`` (>=6 live-subprocess
cases that diff row contents against the bash output; floats 1e-6).

Pipeline map (bash function → Python):
  operator_steering_emit:
    --message / unknown-flag validation (>=2 stderr return code) → emit (raises ValueError)
    env-resolve DB path (MINI_ORK_DB → MINI_ORK_HOME/state.db)     → _resolve_db
    now_ms (gdate → python3 fallback → date fallback)             → _now_ms
    expires_at = now_ms + ttl_secs * 1000                          → emit
    INSERT … NULLIF on run_id / source, return lastrowid           → emit
  operator_steering_fetch_for:
    silent no-op when DB absent                                    → fetch_for (returns [])
    SELECT … ORDER BY severity tier / confidence / created_at, LIMIT 10  → fetch_for
    per-row JSON projection (matches bash JSONL output)            → fetch_for
    mark-consumed UPDATE for returned ids (one statement)          → fetch_for
"""
from __future__ import annotations

import os
import sqlite3
import time

__all__ = [
    "emit",
    "fetch_for",
]


_VALID_ROLES = ("planner", "implementer", "reviewer", "verifier", "any")
_VALID_SEVERITIES = ("info", "warn", "critical")


def _resolve_db(db_path: str | None = None) -> str:
    """Mirror ``_operator_steering_db`` in lib/operator_steering.sh:

        echo "${MINI_ORK_DB:-${MINI_ORK_HOME:-$(pwd)/.mini-ork}/state.db}"

    Three-tier precedence: explicit MINI_ORK_DB → MINI_ORK_HOME/state.db →
    cwd/.mini-ork/state.db. Tests always set MINI_ORK_DB via subprocess
    env= so the cwd fallback rarely fires.
    """
    if db_path:
        return db_path
    if "MINI_ORK_DB" in os.environ and os.environ["MINI_ORK_DB"]:
        return os.environ["MINI_ORK_DB"]
    if "MINI_ORK_HOME" in os.environ and os.environ["MINI_ORK_HOME"]:
        return os.path.join(os.environ["MINI_ORK_HOME"], "state.db")
    return os.path.join(os.getcwd(), ".mini-ork", "state.db")


def _now_ms() -> int:
    """Mirror ``_operator_steering_now_ms`` python3 fallback.

    Bash probes gdate first, then ``python3 -c 'import time; print(int(time.time() * 1000))'``,
    then a coarse ``date +%s * 1000``. The harness test host has no gdate
    (verified — bash subprocess falls through to its own python3 fallback),
    so we mirror that python3 branch directly: ``int(time.time() * 1000)``.
    """
    return int(time.time() * 1000)


def emit(
    message: str,
    run_id: str | None = None,
    role_target: str = "any",
    severity: str = "info",
    source: str = "",
    confidence: float = 0.8,
    ttl_secs: int | None = None,
    **kwargs: object,
) -> int:
    """Mirror ``operator_steering_emit`` in lib/operator_steering.sh.

    Insert a steering row and return the new row id. Raises ``ValueError``
    on validation failures (mirroring bash's ``>&2 'message required'`` /
    ``'unknown flag'`` paths that return exit code 2). Raises
    ``FileNotFoundError`` when ``state.db`` is missing and
    ``sqlite3.OperationalError`` on DB errors — forbidden_fallbacks bans
    silent recovery.

    Validation order (mirrors bash):
      1. unknown kwarg → "unknown flag: <name>" (matches bash `case *)`)
      2. ``message`` non-empty → "--message required"
      3. ``role_target`` in CHECK set
      4. ``severity`` in CHECK set
      5. DB file exists
    """
    if kwargs:
        first = next(iter(kwargs))
        raise ValueError(f"operator_steering_emit: unknown flag: {first}")

    if not message:
        raise ValueError("operator_steering_emit: --message required")

    if role_target not in _VALID_ROLES:
        raise ValueError(
            f"operator_steering_emit: invalid role_target: {role_target!r} "
            f"(expected one of {_VALID_ROLES})"
        )
    if severity not in _VALID_SEVERITIES:
        raise ValueError(
            f"operator_steering_emit: invalid severity: {severity!r} "
            f"(expected one of {_VALID_SEVERITIES})"
        )

    db = _resolve_db()
    if not os.path.isfile(db):
        raise FileNotFoundError(f"operator_steering_emit: state.db not found: {db}")

    if ttl_secs is None:
        # Bash default mirrors ${MINI_ORK_STEERING_DEFAULT_TTL:-3600}; tests
        # leave the env unset so the 3600 default applies to both sides.
        ttl_secs = int(os.environ.get("MINI_ORK_STEERING_DEFAULT_TTL") or "3600")

    now_ms = _now_ms()
    expires_ms = now_ms + int(ttl_secs) * 1000

    con = sqlite3.connect(db, timeout=5.0)
    try:
        con.execute("PRAGMA busy_timeout = 5000")
        cur = con.execute(
            """INSERT INTO operator_steering
                 (run_id, role_target, severity, message, source,
                  confidence, created_at, expires_at)
               VALUES (NULLIF(?, ''), ?, ?, ?, NULLIF(?, ''), ?, ?, ?)""",
            (
                run_id if run_id is not None else "",
                role_target,
                severity,
                message,
                source,
                float(confidence),
                int(now_ms),
                int(expires_ms),
            ),
        )
        con.commit()
        # lastrowid is None only when no row was inserted; INSERT above
        # always succeeds or raises, so it's always set here.
        assert cur.lastrowid is not None
        return int(cur.lastrowid)
    finally:
        con.close()


def fetch_for(
    run_id: str,
    role: str = "any",
    *,
    db_path: str | None = None,
) -> list[dict]:
    """Mirror ``operator_steering_fetch_for`` in lib/operator_steering.sh.

    Returns up to 10 unconsumed, unexpired steering rows for the given
    ``run_id`` + ``role`` (or ``role='any'``), each projected to the same
    dict shape the bash JSONL emits. The matched rows are marked consumed
    in one UPDATE so a second call returns ``[]`` — agents should not
    see the same steering twice in a run.

    Missing DB → returns ``[]`` silently to mirror bash's ``return 0``
    no-op when ``state.db`` is absent (parity test sets ``MINI_ORK_DB``
    to a real file, so this branch rarely fires, but it's there for
    the kickoff's "no fallbacks" constraint: silent return on missing
    DB IS the bash behavior, not a fallback).
    """
    db = _resolve_db(db_path)
    if not os.path.isfile(db):
        return []

    now_ms = _now_ms()
    run_id_arg = run_id or ""

    con = sqlite3.connect(db, timeout=5.0)
    try:
        con.execute("PRAGMA busy_timeout = 5000")
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
            (int(now_ms), run_id_arg, role),
        )
        rows = cur.fetchall()
        if not rows:
            return []

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

        # Mark consumed in one statement — mirrors bash's
        # `UPDATE … SET consumed_at = ? WHERE id IN (…)`.
        placeholders = ",".join("?" for _ in ids)
        con.execute(
            f"UPDATE operator_steering SET consumed_at = ? WHERE id IN ({placeholders})",
            [int(now_ms), *ids],
        )
        con.commit()
        return out
    finally:
        con.close()
