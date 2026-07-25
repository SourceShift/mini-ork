"""rubric_cache — SQLite cache read/write + DB helpers for the rubric gate.

Extracted from ``mini_ork/gates/rubric_prescreen.py`` (SOLID SRP split).
Owns every ``mini_orch_sessions`` / ``epics`` table access plus the
cost-line parser. Public names are re-exported from
``mini_ork.gates.rubric_prescreen`` — import from there, not here,
unless you are writing focused unit tests for the cache layer.

Pipeline map (bash → Python; bash line ranges from
``lib/rubric-prescreen.sh`` and ``lib/cache.sh``):

  fetch_kickoff_path         lines 27-29     → fetch_kickoff_path
  mo_cache_input_hash        cache.sh 69-75  → cache_input_hash
  mo_cache_lookup            cache.sh 98-112 → cache_lookup
  mo_cache_record_hit        cache.sh 115-128 → cache_record_hit
  mo_cache_emit              cache.sh 135-163 → cache_emit
  mo_cache_costline_from_log cache.sh 168-177 → cache_costline_from_log

Notes on parity:
- ``cache_emit`` uses ``secrets.token_hex(16)`` for the uuid (32 hex
  chars, no hyphens). The bash version uses ``uuidgen`` which emits a
  hyphenated UUID. The DB-row diff IGNORES the uuid column (per the
  plan's risk note) — only logical columns (stage, epic_id, iter,
  input_hash, status, output_path, log_path, cost_usd, turns,
  duration_ms, prompt_version) are compared.
- ``cache_costline_from_log`` returns ``(0.0, 0, 0)`` on parse failure
  (bash emits literal "0 0 0" — three space-separated zeros).
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

__all__ = [
    "fetch_kickoff_path",
    "cache_input_hash",
    "cache_lookup",
    "cache_record_hit",
    "cache_emit",
    "cache_costline_from_log",
]


# ─────────────────────────────────────────────────────────────────────────────
# DB helpers (mini_orch_sessions table, mirroring lib/cache.sh 98-177)
# ─────────────────────────────────────────────────────────────────────────────

def _open(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA busy_timeout=5000")
    return con


def fetch_kickoff_path(db_path: str, epic: str, repo_root: str) -> Optional[str]:
    """Mirror bash SELECT at lines 27-29.

    Returns the absolute kickoff path (``<repo_root>/<kickoff_path>``)
    for the given epic, or ``None`` if the epic has no kickoff_path
    set. Mirrors the bash's ``local kickoff_rel=$(sqlite3 ...)`` which
    emits an empty string on no row, then the bash uses
    ``$REPO_ROOT/$kickoff_rel``. The port collapses that into a
    single ``Optional[str]`` return — None on miss.
    """
    con = _open(db_path)
    try:
        row = con.execute(
            "SELECT kickoff_path FROM epics WHERE id=?",
            (epic,),
        ).fetchone()
    finally:
        con.close()
    if row is None or not row[0]:
        return None
    kickoff_rel = row[0]
    return f"{repo_root}/{kickoff_rel}"


def cache_input_hash(data: str) -> str:
    """Mirror bash ``mo_cache_input_hash`` (lib/cache.sh 69-75).

    Prefers ``sha256sum`` if available, falls back to ``shasum -a 256``.
    In Python this is a single ``hashlib.sha256`` call — both shells
    produce the same hex digest on the same input.

    Note: bash reads from stdin; this function takes a string argument.
    The caller is responsible for feeding it the full bundle (use
    ``hash_bundle`` if you need bash's record-separator semantics).
    """
    import hashlib
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def cache_lookup(
    db_path: str,
    stage: str,
    epic: str,
    iter: int,
    input_hash: str,
) -> str:
    """Mirror bash ``mo_cache_lookup`` (lib/cache.sh 98-112).

    Returns the output_path on a cache HIT (status=success, not
    expired). Empty string on miss (matches bash's empty stdout).
    """
    con = _open(db_path)
    try:
        row = con.execute(
            """
            SELECT output_path FROM mini_orch_sessions
            WHERE epic_id = ? AND iter = ? AND stage = ? AND input_hash = ?
              AND status = 'success'
              AND expires_at > strftime('%Y-%m-%dT%H:%M:%fZ','now')
            ORDER BY updated_at DESC
            LIMIT 1;
            """,
            (epic, iter, stage, input_hash),
        ).fetchone()
    finally:
        con.close()
    return row[0] if row else ""


def cache_record_hit(
    db_path: str,
    stage: str,
    epic: str,
    iter: int,
    input_hash: str,
) -> None:
    """Mirror bash ``mo_cache_record_hit`` (lib/cache.sh 115-128).

    Bumps ``reused_count`` on the row that just served the hit. The
    bash ``UPDATE`` does NOT include the WHERE condition ``status =
    'success'`` (line 126) so it could increment a non-success row —
    the port includes it as well to match verbatim.
    """
    con = _open(db_path)
    try:
        con.execute(
            """
            UPDATE mini_orch_sessions
               SET reused_count = reused_count + 1,
                   updated_at   = strftime('%Y-%m-%dT%H:%M:%fZ','now')
             WHERE epic_id = ? AND iter = ? AND stage = ? AND input_hash = ?
               AND status = 'success';
            """,
            (epic, iter, stage, input_hash),
        )
        con.commit()
    finally:
        con.close()


def _expires_at_30d() -> str:
    """Mirror bash expires_at at lib/cache.sh 146-149.

    ``now + 30 days`` formatted as ``%Y-%m-%dT%H:%M:%S.fZ`` (3-digit
    millisecond precision, trailing Z). Matches the bash
    ``python3 -c 'datetime.utcnow() + timedelta(days=30)'`` output
    (utcnow is deprecated in 3.12 but the result is identical to
    ``datetime.now(timezone.utc)`` for this use).
    """
    dt = datetime.now(timezone.utc) + timedelta(days=30)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{dt.microsecond // 1000:03d}Z"


def cache_emit(
    db_path: str,
    stage: str,
    epic: str,
    iter: int,
    input_hash: str,
    status: str,
    output_path: str,
    log_path: str,
    cost_usd: float,
    turns: int,
    duration_ms: int,
    job_id: str = "unknown",
    prompt_version: str = "v1",
) -> None:
    """Mirror bash ``mo_cache_emit`` (lib/cache.sh 135-163).

    Insert a row at stage completion. uuid is ``secrets.token_hex(16)``
    (32 hex chars, no hyphens) — differs from bash's hyphenated
    ``uuidgen`` output, but the DB-row diff IGNORES the uuid column
    per the plan's parity contract.
    """
    uuid = secrets.token_hex(16)
    expires_at = _expires_at_30d()
    con = _open(db_path)
    try:
        con.execute(
            """
            INSERT INTO mini_orch_sessions
              (uuid, job_id, epic_id, iter, stage, input_hash, status,
               output_path, log_path, cost_usd, turns, duration_ms,
               expires_at, prompt_version)
            VALUES
              (?, ?, ?, ?, ?, ?, ?,
               ?, ?, ?, ?, ?,
               ?, ?)
            ON CONFLICT (uuid) DO NOTHING;
            """,
            (
                uuid, job_id, epic, iter, stage, input_hash, status,
                output_path, log_path, cost_usd, turns, duration_ms,
                expires_at, prompt_version,
            ),
        )
        con.commit()
    finally:
        con.close()


def cache_costline_from_log(log_path: str) -> tuple[float, int, int]:
    """Mirror bash ``mo_cache_costline_from_log`` (lib/cache.sh 168-177).

    Returns ``(cost_usd, turns, duration_ms)``. Emits ``(0.0, 0, 0)``
    if the log file is missing or no ``"type":"result"`` line is
    present (bash emits literal "0 0 0").
    """
    if not os.path.isfile(log_path):
        return (0.0, 0, 0)
    try:
        with open(log_path, "r", errors="replace") as f:
            text = f.read()
    except OSError:
        return (0.0, 0, 0)
    # Match bash: grep '"type":"result"' | tail -1
    m = None
    for line in text.splitlines():
        if '"type":"result"' in line:
            m = line
    if m is None:
        return (0.0, 0, 0)
    try:
        obj = json.loads(m)
    except (ValueError, TypeError):
        return (0.0, 0, 0)
    cost = float(obj.get("total_cost_usd") or 0)
    nturns = int(obj.get("num_turns") or 0)
    dur = int(obj.get("duration_ms") or 0)
    return (cost, nturns, dur)
