"""Stage-level memoization cache — Python port of lib/cache.sh (trunk, Tier A).

Content-addressed cache: each stage emits a row keyed by
``(epic_id, stage, input_hash)``. A re-dispatch with the same input bundle hits
the cache and skips the LLM/verifier call.

MIGRATION NOTE — win #2 (cross-iteration reuse):
    The bash version put ``iter`` in the lookup predicate, so an identical
    ``input_hash`` produced at iteration 2 could NOT match the row emitted at
    iteration 1. In the ×5 recursion loop that meant every verifier tier and the
    4-model LLM panel fully recomputed each iteration even on byte-identical
    inputs. This port drops ``iter`` from the MATCH predicate — it is still
    stored for provenance — so a stable region is embedded once and reused
    across iterations. The allowed ``stage`` set is also widened to include the
    verifier tiers + panel so the loop's expensive nodes become cacheable.

Faithful in every other respect: same table, same sha256 bundle hashing
(0x1e record separator), same 30-day TTL, same reused_count accounting.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import uuid as _uuid
from datetime import datetime, timedelta, timezone

# Widened vs bash: added the recursion-loop stages so they can be cached.
ALLOWED_STAGES = frozenset({
    "spec-author", "spec-reviewer", "mutation-adversary", "mutation-validator",
    "rubric", "worker", "reviewer", "bdd-runner", "reflection-refiner",
    # win #2 additions — the expensive recursion-loop nodes:
    "implementer", "verifier", "tier1", "tier2", "tier3", "tier4-panel",
})
_RS = "\x1e"  # record separator, matches bash $'\x1e'


def db_path(db: str | None = None) -> str:
    """MINI_ORK_DB → MINI_ORK_HOME/state.db → .mini-ork/state.db (matches bash)."""
    if db:
        return db
    env = os.environ.get("MINI_ORK_DB")
    if env:
        return env
    home = os.environ.get("MINI_ORK_HOME", ".mini-ork")
    return os.path.join(home, "state.db")


def _conn(db: str | None) -> sqlite3.Connection:
    return sqlite3.connect(db_path(db))


def init_schema(db: str | None = None) -> None:
    """Create the table + stats view if missing. Idempotent."""
    stage_list = ",".join(f"'{s}'" for s in sorted(ALLOWED_STAGES))
    con = _conn(db)
    try:
        con.executescript(
            f"""
CREATE TABLE IF NOT EXISTS mini_orch_sessions (
  uuid TEXT PRIMARY KEY, job_id TEXT NOT NULL, epic_id TEXT NOT NULL,
  iter INTEGER NOT NULL, stage TEXT NOT NULL CHECK (stage IN ({stage_list})),
  input_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('running','success','failed','resumable')),
  output_path TEXT, log_path TEXT, cost_usd NUMERIC, turns INTEGER,
  duration_ms INTEGER,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  expires_at TEXT NOT NULL, reused_count INTEGER NOT NULL DEFAULT 0,
  prompt_version TEXT
);
-- win #2: index no longer leads with iter, so content-hash matches span iters.
CREATE INDEX IF NOT EXISTS mos_lookup2 ON mini_orch_sessions (
  epic_id, stage, input_hash, status, expires_at
);
CREATE INDEX IF NOT EXISTS mos_gc ON mini_orch_sessions (expires_at);
"""
        )
        con.commit()
    finally:
        con.close()


def input_hash(data: bytes | str) -> str:
    """sha256 hex of the input (parity with `sha256sum | awk '{print $1}'`)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def hash_bundle(*args: str) -> str:
    """Combine files (by content) or literal strings into the canonical hash
    bundle, each followed by a 0x1e separator — byte-identical to bash."""
    parts: list[str] = []
    for arg in args:
        if os.path.isfile(arg):
            with open(arg, encoding="utf-8", errors="surrogateescape") as fh:
                parts.append(fh.read())
        else:
            parts.append(arg)
        parts.append(_RS)
    return input_hash("".join(parts))


def lookup(stage: str, epic: str, input_hash_: str,
           iter: int | None = None, db: str | None = None) -> str | None:
    """Return output_path on a cache HIT (success + unexpired), else None.

    win #2: matches on (epic, stage, input_hash) ONLY — ``iter`` is accepted
    for call-site compatibility but deliberately NOT part of the predicate, so a
    hash computed in one iteration matches a row emitted in an earlier one.
    """
    con = _conn(db)
    try:
        row = con.execute(
            """SELECT output_path FROM mini_orch_sessions
                WHERE epic_id=? AND stage=? AND input_hash=? AND status='success'
                  AND expires_at > strftime('%Y-%m-%dT%H:%M:%fZ','now')
                ORDER BY updated_at DESC LIMIT 1""",
            (epic, stage, input_hash_),
        ).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def record_hit(stage: str, epic: str, input_hash_: str, db: str | None = None) -> None:
    """Bump reused_count on the row that served the hit (iter-agnostic)."""
    con = _conn(db)
    try:
        con.execute(
            """UPDATE mini_orch_sessions
                  SET reused_count=reused_count+1,
                      updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE epic_id=? AND stage=? AND input_hash=? AND status='success'""",
            (epic, stage, input_hash_),
        )
        con.commit()
    finally:
        con.close()


def emit(stage: str, epic: str, iter: int, input_hash_: str, status: str,
         output_path: str, log_path: str, cost_usd: float = 0.0, turns: int = 0,
         duration_ms: int = 0, prompt_version: str = "v1",
         db: str | None = None) -> None:
    """Insert a row at stage completion. ``iter`` is stored for provenance
    (not used for matching). expires_at = now + 30 days."""
    expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    con = _conn(db)
    try:
        con.execute(
            """INSERT INTO mini_orch_sessions
                 (uuid, job_id, epic_id, iter, stage, input_hash, status,
                  output_path, log_path, cost_usd, turns, duration_ms,
                  expires_at, prompt_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (uuid) DO NOTHING""",
            (str(_uuid.uuid4()), os.environ.get("JOB_ID", "unknown"), epic, iter,
             stage, input_hash_, status, output_path, log_path, cost_usd, turns,
             duration_ms, expires_at, prompt_version),
        )
        con.commit()
    finally:
        con.close()


def gc(db: str | None = None) -> int:
    """Delete expired rows. Returns the number removed."""
    con = _conn(db)
    try:
        cur = con.execute(
            "DELETE FROM mini_orch_sessions "
            "WHERE expires_at <= strftime('%Y-%m-%dT%H:%M:%fZ','now')")
        con.commit()
        return cur.rowcount or 0
    finally:
        con.close()


def run_summary(job_id: str, db: str | None = None) -> str:
    """Port of ``mo_cache_run_summary`` (lib/cache.sh) — stats table for
    COMPLETION_REPORT.md.

    Bash runs ``sqlite3 "$db" -column -header <SQL> 2>/dev/null`` and lets the
    CLI render the columnar table. The ``-column`` alignment is
    sqlite3-version-dependent, so the faithful native port delegates the
    rendering to the same ``sqlite3`` CLI (no bash): same binary, same SQL,
    byte-identical stdout. Returns "" when sqlite3 is missing or errors —
    matching bash's stderr-suppressed silent-empty behaviour.
    """
    resolved = db_path(db)
    sql = (
        "SELECT\n"
        "      stage,\n"
        "      SUM(reused_count) AS reuses,\n"
        "      ROUND(SUM(CASE WHEN reused_count > 0 THEN cost_usd * reused_count ELSE 0 END), 2) AS saved_usd\n"
        "    FROM mini_orch_sessions\n"
        f"    WHERE job_id = '{job_id}' AND reused_count > 0\n"
        "    GROUP BY stage;"
    )
    try:
        proc = subprocess.run(
            ["sqlite3", resolved, "-column", "-header", sql],
            capture_output=True, text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout
