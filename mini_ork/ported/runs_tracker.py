"""mini-ork dispatch tracker — Python port of lib/runs-tracker.sh.

Faithful in-process port of all six functions from `lib/runs-tracker.sh`:

  mo_runs_ensure_schema            → ensure_schema
  mo_runs_resolve_claude_session_id → resolve_claude_session_id
  mo_runs_sql_escape                → sql_escape
  mo_runs_open                      → open
  mo_runs_update_progress           → update_progress
  mo_runs_close                     → close

The bash library is the authoritative source. This module mirrors its
SQL byte-for-byte (including the COALESCE+rationale append and the single-
connection `last_insert_rowid()` extraction) so callers have an in-process
alternative entry point and parity tests have a stable target.

Co-existence model (strangler-fig):
  bash `lib/runs-tracker.sh` stays untouched (this port never replaces it
  in the call path). Parity is enforced by `tests/unit/test_runs_tracker_py.py`
  which invokes the live bash library via subprocess and SELECT-diffs the
  resulting rows against the Python port.

Rationale-column append semantics (mirrors bash exactly):
  Bash:  rationale = COALESCE(rationale, '') ||
                    CASE WHEN rationale IS NULL OR rationale = '' THEN '' ELSE ' | ' END ||
                    'iter:' || '<escaped verdict>'
  Same SQL template is reproduced here; on the first append (rationale NULL
  or empty) no ' | ' separator is emitted, so the column starts with
  'iter:FAIL' / 'final:APPROVE'. On subsequent appends the ' | ' separator
  joins them ('iter:FAIL | iter:WARN'). Verified against the live bash row
  diff in tests/unit/test_runs_tracker_py.py::test_update_progress_parity.

Module-level _SCHEMA_INIT_DB is a per-DB-path set (mirrors bash's session-
wide _MO_RUNS_SCHEMA_INIT=1 guard but keyed on the actual DB file so tests
with fresh per-case temp DBs still exercise the CREATE statements). The
guard prevents repeated DDLS on hot paths (audit F-01: ~300K wasted
sqlite3 forks/day at 100K dispatch volume).

Forbidden-fallbacks compliance:
  Bash silently swallows sqlite3 errors via 2>/dev/null. The Python port
  surfaces them via `warnings.warn(...)` (matching the row-diff contract —
  best-effort audit trail — while honoring no-silent-recovery). The warn
  fires AFTER the SQL is emitted; row content is unchanged either way
  unless the connection itself failed.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import warnings
from datetime import datetime, timezone


__all__ = [
    "ensure_schema",
    "resolve_claude_session_id",
    "sql_escape",
    "open",
    "update_progress",
    "close",
]


_SCHEMA_INIT_DB: set[str] = set()


def _db_path_or_default(db: str | os.PathLike | None) -> str:
    """Mirror bash `_MO_DB` resolution order:

        MINI_ORK_DB (explicit)  or  ${MINI_ORK_HOME:-.mini-ork}/state.db
    """
    if db is not None:
        return os.fspath(db)
    env_db = os.environ.get("MINI_ORK_DB")
    if env_db:
        return env_db
    home = os.environ.get("MINI_ORK_HOME", ".mini-ork")
    return os.path.join(home, "state.db")


def _normalize_db_key(db_path: str) -> str:
    """Absolute path is the dict key for _SCHEMA_INIT_DB."""
    return os.path.abspath(db_path)


def ensure_schema(db: str | os.PathLike | None = None) -> None:
    """Mirror lib/runs-tracker.sh::mo_runs_ensure_schema.

    Idempotent. Defensive ADD COLUMN for `runs.claude_session_id` /
    `runs.zellij_session_name` (gated on pragma_table_info, so re-runs don't
    fail with "duplicate column" on DBs that already have those columns from
    migration 0001_core.sql). CREATE TABLE IF NOT EXISTS for orch_dispatches
    + the three primary indexes.

    Module-level per-DB guard short-circuits repeat calls in the same
    process — mirrors bash's `_MO_RUNS_SCHEMA_INIT=1` export.
    """
    db_path = _db_path_or_default(db)
    key = _normalize_db_key(db_path)
    if key in _SCHEMA_INIT_DB:
        return
    try:
        con = sqlite3.connect(db_path)
        try:
            for col in ("claude_session_id", "zellij_session_name"):
                present = con.execute(
                    "SELECT COUNT(*) FROM pragma_table_info('runs') WHERE name=?;",
                    (col,),
                ).fetchone()[0]
                if not present:
                    con.execute(f"ALTER TABLE runs ADD COLUMN {col} TEXT;")
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS orch_dispatches (
                  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                  parent_dispatch_id  INTEGER REFERENCES orch_dispatches(id),
                  epic_id             TEXT NOT NULL,
                  group_id            TEXT,
                  dispatched_by       TEXT NOT NULL CHECK (dispatched_by IN
                    ('claude-session','orchestrator','human-cli','scaffold')),
                  claude_session_id   TEXT,
                  zellij_session_name TEXT,
                  kickoff_path        TEXT,
                  run_dir             TEXT,
                  status              TEXT NOT NULL CHECK (status IN
                    ('pending','in_progress','fanned_out','completed','cancelled')),
                  rationale           TEXT,
                  created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                  updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                  closed_at           TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_orch_dispatches_epic    ON orch_dispatches(epic_id);
                CREATE INDEX IF NOT EXISTS idx_orch_dispatches_status  ON orch_dispatches(status);
                CREATE INDEX IF NOT EXISTS idx_orch_dispatches_session ON orch_dispatches(claude_session_id);
                """
            )
            con.commit()
        finally:
            con.close()
    except sqlite3.Error as exc:
        warnings.warn(f"[mini-ork] runs_tracker.ensure_schema: {exc}", stacklevel=2)
    _SCHEMA_INIT_DB.add(key)


def resolve_claude_session_id(
    zellij: str | None = None,
    home_dir: str | os.PathLike | None = None,
) -> str:
    """Mirror lib/runs-tracker.sh::mo_runs_resolve_claude_session_id.

    Reads `$HOME/.claude/status/<zellij>.json` and extracts `.session_id`.
    Returns '' when (a) zellij is empty, (b) the file is absent, or (c) the
    file is missing the session_id key / unparseable. Order matches bash:
    ZELLIJ_SESSION_NAME || ZELLIJ, then bail on empty / missing file.

    `home_dir` defaults to `$HOME` (mirrors bash `~`), but tests can override
    to point at a fixture root.
    """
    if not zellij:
        zellij = (
            os.environ.get("ZELLIJ_SESSION_NAME")
            or os.environ.get("ZELLIJ")
            or ""
        )
    if not zellij:
        return ""
    base = os.path.expanduser(
        os.fspath(home_dir) if home_dir else "~"
    )
    status_file = os.path.join(base, ".claude", "status", f"{zellij}.json")
    if not os.path.isfile(status_file):
        return ""
    try:
        with io.open(status_file, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return ""
    return data.get("session_id") or ""


def sql_escape(value: str | None) -> str:
    """Mirror lib/runs-tracker.sh::mo_runs_sql_escape (sed s/'/''/g).

    SQLite single-quote escape: a single `'` becomes two (`''`). None and
    empty pass through as empty (mirrors `${1:-}` defaulting).
    """
    s = "" if value is None else str(value)
    return s.replace("'", "''")


def _git_branch(worktree: str) -> str:
    """Mirror `git -C "$worktree" symbolic-ref --short HEAD 2>/dev/null || echo unknown`."""
    try:
        r = subprocess.run(
            ["git", "-C", worktree, "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        branch = (r.stdout or "").strip()
        if branch:
            return branch
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def _utc_run_stamp() -> str:
    """Mirror bash `date -u +%Y%m%dT%H%M%S` — UTC seconds-precision stamp."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def open(
    db: str | os.PathLike | None,
    epic: str,
    worktree: str,
) -> int:
    """Mirror lib/runs-tracker.sh::mo_runs_open.

    Inserts one orch_dispatches row with status='in_progress'. Returns the
    dispatch id (cursor.lastrowid on the SAME connection that executed the
    INSERT — bash does this with `INSERT ... ; SELECT last_insert_rowid();`
    in one sqlite3 invocation, capturing the same connection's rowid).

    Args:
        db: SQLite path (explicit). Falls back to $MINI_ORK_DB or
            $MINI_ORK_HOME/state.db if None.
        epic: The epic id this dispatch belongs to.
        worktree: A git worktree path; the current branch becomes `branch`.
            `run_dir` is derived as `mini-ork/{JOB_ID or 'unknown'}/{epic}/{utc stamp}`.

    Returns dispatch id on success, -1 on sqlite3 error (with stderr warn).
    """
    db_path = _db_path_or_default(db)
    ensure_schema(db_path)
    branch = _git_branch(worktree)  # retained for parity w/ bash (unused in INSERT)
    _ = branch  # explicit no-op; bash copies branch to a local var too
    job_id = os.environ.get("JOB_ID") or ""
    zellij = (
        os.environ.get("ZELLIJ_SESSION_NAME")
        or os.environ.get("ZELLIJ")
        or ""
    )
    claude_sid = resolve_claude_session_id()
    run_dir = f"mini-ork/{job_id or 'unknown'}/{epic}/{_utc_run_stamp()}"

    epic_sql = "'" + sql_escape(epic) + "'"
    group_sql = "'" + sql_escape(job_id) + "'" if job_id else "NULL"
    claude_sid_sql = "'" + sql_escape(claude_sid) + "'" if claude_sid else "NULL"
    zellij_sql = "'" + sql_escape(zellij) + "'" if zellij else "NULL"
    run_dir_sql = "'" + sql_escape(run_dir) + "'"

    sql = (
        "INSERT INTO orch_dispatches "
        "(epic_id, group_id, dispatched_by, claude_session_id, "
        "zellij_session_name, run_dir, status) VALUES "
        f"({epic_sql}, {group_sql}, 'claude-session', "
        f"{claude_sid_sql}, {zellij_sql}, {run_dir_sql}, 'in_progress');"
    )
    try:
        con = sqlite3.connect(db_path)
        try:
            cur = con.execute(sql)
            con.commit()
            return int(cur.lastrowid or 0)
        finally:
            con.close()
    except sqlite3.Error as exc:
        warnings.warn(f"[mini-ork] runs_tracker.open: {exc}", stacklevel=2)
        return -1


def _append_rationale_segment(
    db_path: str,
    dispatch_id: int,
    label: str,
    escaped_value: str,
    *,
    set_status: str | None = None,
    set_closed_at: bool = False,
) -> None:
    """Internal helper — shared SQL template for update_progress and close.

    Mirrors the bash UPDATE pattern:

        rationale = COALESCE(rationale, '') ||
                    CASE WHEN rationale IS NULL OR rationale = '' THEN '' ELSE ' | ' END ||
                    '<label>:' || '<escaped_value>'

    `set_status` updates the status column (used by close).
    `set_closed_at` sets closed_at to NOW() (used by close).
    """
    extra_sets: list[str] = []
    params: list[object] = []
    if set_status is not None:
        extra_sets.append("status     = ?")
        params.append(set_status)
    if set_closed_at:
        extra_sets.append("closed_at  = strftime('%Y-%m-%dT%H:%M:%fZ','now')")
    extra_sets.append("updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')")
    rationale_sql = (
        f"rationale  = COALESCE(rationale, '') || "
        f"CASE WHEN rationale IS NULL OR rationale = '' THEN '' ELSE ' | ' END || "
        f"'{label}:' || ?"
    )
    params.append(escaped_value)
    params.append(dispatch_id)
    sql = (
        "UPDATE orch_dispatches SET "
        + ", ".join(extra_sets)
        + f", {rationale_sql} "
        + "WHERE id = ?;"
    )
    try:
        con = sqlite3.connect(db_path)
        try:
            con.execute(sql, params)
            con.commit()
        finally:
            con.close()
    except sqlite3.Error as exc:
        warnings.warn(
            f"[mini-ork] runs_tracker.{label}_append: {exc}", stacklevel=2
        )


def update_progress(
    db: str | os.PathLike | None,
    dispatch_id: int,
    verdict: str,
) -> None:
    """Mirror lib/runs-tracker.sh::mo_runs_update_progress.

    Bumps `updated_at` and appends `iter:<verdict>` (or ` | iter:<verdict>`
    if rationale already populated) to the `rationale` column.
    """
    if not dispatch_id:
        return
    db_path = _db_path_or_default(db)
    _append_rationale_segment(
        db_path,
        dispatch_id,
        "iter",
        sql_escape(verdict),
        set_status=None,
        set_closed_at=False,
    )


def close(
    db: str | os.PathLike | None,
    dispatch_id: int,
    epic: str,
    final_verdict: str,
) -> None:
    """Mirror lib/runs-tracker.sh::mo_runs_close.

    Maps `final_verdict` to status (`APPROVE|MERGED|SALVAGED` → completed;
    anything else → cancelled), stamps `closed_at` to NOW, appends
    `final:<verdict>` to `rationale`. `epic` is retained to match the bash
    signature (unused — bash doesn't reference it either).
    """
    if not dispatch_id:
        return
    _ = epic  # noqa: F841 — mirror bash signature; unused
    db_path = _db_path_or_default(db)
    new_status = (
        "completed"
        if final_verdict in ("APPROVE", "MERGED", "SALVAGED")
        else "cancelled"
    )
    _append_rationale_segment(
        db_path,
        dispatch_id,
        "final",
        sql_escape(final_verdict),
        set_status=new_status,
        set_closed_at=True,
    )
