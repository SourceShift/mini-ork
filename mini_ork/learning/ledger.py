"""Learning injection + pass-stats ledger.

Persists what each LLM node was actually given (per-source rows in
``lesson_injections``) and how each learning pipeline pass fared
(``learning_pass_stats``). The IDE reads these tables to show
"this lesson was used N times" and a per-stage health strip.

Both tables are write-only in this phase. The readers
(``injection_counts``, ``stage_health``) are the contract surface the IDE
exposure phase (P1b) calls into.

Hard rule: writers NEVER raise on the dispatch path. A failure here prints one
line to stderr and returns 0 / None. A re-dispatch loop is the most expensive
thing a learner can trigger — a DB write that interrupts it is worse than no
DB write at all.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from typing import Iterable


_DEFAULT_HOME = ".mini-ork"
_MAX_ERROR_CHARS = 500


def _resolve_db(db: str | None) -> str | None:
    """Resolve the DB path: explicit arg, then ``MINI_ORK_DB``, then
    ``$MINI_ORK_HOME/state.db``, then ``.mini-ork/state.db``. Returns a path
    string in every case (the caller's passed path or the env-defaulted one);
    "skip" is decided by ``_open`` (path missing → None) and
    ``ensure_schema`` (no such table → silent warning), not here.

    The pattern mirrors ``cost_ledger.spent_last_24h`` (no RunContext
    dependency) so this module stays a pure leaf under ``mini_ork/learning/``.
    """
    if db:
        return db
    env_db = os.environ.get("MINI_ORK_DB")
    if env_db:
        return env_db
    home = os.environ.get("MINI_ORK_HOME") or _DEFAULT_HOME
    return os.path.join(home, "state.db")


def _open(db: str | None) -> sqlite3.Connection | None:
    """Open a connection with PRAGMA busy_timeout=5000. Returns None if the
    resolved path does not exist on disk OR is unwritable — callers fall
    through to a silent return rather than propagating.

    Strict on missing. A missing file is not conjured: ``sqlite3.connect``
    creates it on open, which silently turns a "the migration hasn't been
    applied yet" environment into "the writer just made a new DB" — a
    corruption-class bug for the readers (injection_counts, stage_health)
    that have to assume the migration ran. The kickoff wants the writers
    silent on a missing DB, not to bootstrap one.
    """
    path = _resolve_db(db)
    if not path or not os.path.exists(path):
        return None
    try:
        con = sqlite3.connect(path, timeout=5.0)
    except (sqlite3.Error, OSError):
        print(f"  [warn] ledger: cannot open {path}", file=sys.stderr)
        return None
    try:
        con.execute("PRAGMA busy_timeout=5000")
    except sqlite3.Error:
        pass
    return con


def ensure_schema(db: str | None = None) -> None:
    """Idempotent CREATE TABLE + CREATE INDEX for both ledger tables.

    Production DBs have the migration applied (``0062_learning_ledger.sql``);
    this function is the bridge for tests, fresh dev DBs, and a crashed
    migrator that left the schema behind. Never raises — a CREATE failure
    is a warning, not a stop-the-line event.
    """
    con = _open(db)
    if con is None:
        return
    try:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS lesson_injections (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id       TEXT    NOT NULL,
                node_id      TEXT    NOT NULL,
                node_type    TEXT,
                lane         TEXT,
                task_class   TEXT,
                attempt      INTEGER,
                source_kind  TEXT    NOT NULL,
                source_id    TEXT    NOT NULL,
                held_out     INTEGER NOT NULL DEFAULT 0,
                ts           INTEGER NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_lesson_injections_unique
              ON lesson_injections(run_id, node_id, attempt, source_kind, source_id, held_out);
            CREATE INDEX IF NOT EXISTS idx_lesson_injections_source
              ON lesson_injections(source_kind, source_id);
            CREATE INDEX IF NOT EXISTS idx_lesson_injections_run_node
              ON lesson_injections(run_id, node_id);
            CREATE TABLE IF NOT EXISTS learning_pass_stats (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                pass_id    TEXT    NOT NULL,
                ts         INTEGER NOT NULL,
                stage      TEXT    NOT NULL,
                inputs     INTEGER,
                outputs    INTEGER,
                failures   INTEGER,
                lane       TEXT,
                last_error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_learning_pass_stats_stage_ts
              ON learning_pass_stats(stage, ts);
            """
        )
        con.commit()
    except sqlite3.Error as e:
        print(f"  [warn] ledger: ensure_schema failed: {e}", file=sys.stderr)
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass


def record_injections(
    run_id: str,
    node_id: str,
    node_type: str | None,
    lane: str | None,
    task_class: str | None,
    attempt: int | None,
    sources: Iterable[dict] | None,
    *,
    held_out: bool = False,
    ts: int | None = None,
    db: str | None = None,
) -> int:
    """One row per source that has both ``kind`` and ``id``. INSERT OR IGNORE
    so a retry is a no-op (the unique key dedupes). Returns the number of
    rows actually inserted.

    Sources missing ``kind`` or ``id`` are skipped — they were the
    "no-id lesson" case the carrier logged but cannot count toward a lesson
    counter (kickoff:71).
    """
    if not sources:
        return 0
    ensure_schema(db)
    con = _open(db)
    if con is None:
        return 0
    when = int(ts) if ts is not None else int(time.time())
    rows = []
    for src in sources:
        if not isinstance(src, dict):
            continue
        kind = src.get("kind")
        sid = src.get("id")
        if not kind or not sid:
            continue
        rows.append(
            (
                run_id, node_id, node_type, lane, task_class,
                int(attempt) if attempt is not None else None,
                str(kind), str(sid), 1 if held_out else 0, when,
            )
        )
    if not rows:
        return 0
    try:
        cur = con.executemany(
            """
            INSERT OR IGNORE INTO lesson_injections
                (run_id, node_id, node_type, lane, task_class, attempt,
                 source_kind, source_id, held_out, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        con.commit()
        return int(cur.rowcount or 0)
    except sqlite3.Error as e:
        print(f"  [warn] ledger: record_injections failed: {e}", file=sys.stderr)
        return 0
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass


def record_pass_stat(
    pass_id: str,
    stage: str,
    *,
    inputs: int = 0,
    outputs: int = 0,
    failures: int = 0,
    lane: str = "",
    last_error: str = "",
    ts: int | None = None,
    db: str | None = None,
) -> None:
    """One row per pass execution. ``last_error`` is trimmed to its last
    ``_MAX_ERROR_CHARS`` chars so a noisy stack trace cannot bloat the DB.

    Always None on the dispatch path — never raises.
    """
    ensure_schema(db)
    con = _open(db)
    if con is None:
        return
    when = int(ts) if ts is not None else int(time.time())
    err = (last_error or "")[-_MAX_ERROR_CHARS:]
    try:
        con.execute(
            """
            INSERT INTO learning_pass_stats
                (pass_id, ts, stage, inputs, outputs, failures, lane, last_error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pass_id,
                when,
                stage,
                int(inputs),
                int(outputs),
                int(failures),
                lane or "",
                err,
            ),
        )
        con.commit()
    except sqlite3.Error as e:
        print(f"  [warn] ledger: record_pass_stat failed: {e}", file=sys.stderr)
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass


def injection_counts(
    source_kind: str,
    source_ids: Iterable[str],
    *,
    since: int | None = None,
    db: str | None = None,
) -> dict[str, dict]:
    """Read-only. ``{source_id: {"uses", "held_out", "last_ts", "runs"}}``.

    ``uses`` = total injections (including held_out), ``held_out`` = those
    marked held_out=1, ``last_ts`` = most recent injection ts, ``runs`` =
    distinct run_id count. A source_id with zero rows is absent from the
    returned dict (not present-with-zeros).
    """
    ids = [str(s) for s in source_ids]
    if not ids:
        return {}
    con = _open(db)
    if con is None:
        return {}
    try:
        placeholders = ",".join("?" for _ in ids)
        params: list = [source_kind, *ids]
        sql = (
            f"SELECT source_id, COUNT(*), "
            f"SUM(CASE WHEN held_out=1 THEN 1 ELSE 0 END), "
            f"MAX(ts), COUNT(DISTINCT run_id) "
            f"FROM lesson_injections WHERE source_kind = ? "
            f"AND source_id IN ({placeholders})"
        )
        if since is not None:
            sql += " AND ts >= ?"
            params.append(int(since))
        sql += " GROUP BY source_id"
        out: dict[str, dict] = {}
        for sid, uses, held, last_ts, runs in con.execute(sql, params):
            out[str(sid)] = {
                "uses": int(uses or 0),
                "held_out": int(held or 0),
                "last_ts": int(last_ts or 0),
                "runs": int(runs or 0),
            }
        return out
    except sqlite3.Error as e:
        print(f"  [warn] ledger: injection_counts failed: {e}", file=sys.stderr)
        return {}
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass


def stage_health(
    *,
    window_passes: int = 3,
    db: str | None = None,
) -> list[dict]:
    """Read-only. Per stage: the last ``window_passes`` rows newest-first,
    plus ``"alarm": True`` when ``outputs == 0`` in ALL window rows AND
    ``inputs > 0`` in at least one (kickoff:51-59).

    ``"last_error"`` is taken from the newest row with failures > 0 in the
    window (None if no failures).

    Stages with zero rows in the window are absent from the returned list.
    """
    if window_passes <= 0:
        return []
    con = _open(db)
    if con is None:
        return []
    try:
        stages = [
            row[0]
            for row in con.execute(
                "SELECT DISTINCT stage FROM learning_pass_stats"
            )
        ]
        out: list[dict] = []
        for stage in stages:
            rows = list(
                con.execute(
                    "SELECT pass_id, ts, inputs, outputs, failures, lane, last_error "
                    "FROM learning_pass_stats WHERE stage = ? "
                    "ORDER BY ts DESC LIMIT ?",
                    (stage, int(window_passes)),
                )
            )
            if not rows:
                continue
            window = [
                {
                    "pass_id": r[0],
                    "ts": int(r[1] or 0),
                    "inputs": int(r[2] or 0),
                    "outputs": int(r[3] or 0),
                    "failures": int(r[4] or 0),
                    "lane": r[5] or "",
                    "last_error": r[6] or "",
                }
                for r in rows
            ]
            any_inputs = any(w["inputs"] > 0 for w in window)
            all_zero_outputs = all(w["outputs"] == 0 for w in window)
            err = next(
                (w["last_error"] for w in window if w["failures"] > 0),
                None,
            )
            out.append(
                {
                    "stage": stage,
                    "window": window,
                    "alarm": bool(any_inputs and all_zero_outputs),
                    "last_error": err,
                }
            )
        return out
    except sqlite3.Error as e:
        print(f"  [warn] ledger: stage_health failed: {e}", file=sys.stderr)
        return []
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass
