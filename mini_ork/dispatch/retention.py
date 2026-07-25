"""Retention helpers for run_artifacts (kickoff feat/run-artifacts-store).

Two functions, both best-effort and idempotent:

  - :func:`gzip_run_stream` walks ``$MINI_ORK_RUN_DIR``, gzips every
    ``agent-*.stream.jsonl`` to ``agent-*.stream.jsonl.gz``, and rewrites the
    matching ``run_artifacts`` row's rel_path/sha256/bytes to the .gz file.

  - :func:`prune_old_trajectories` deletes ``run_artifacts`` rows whose kind
    is the per-call ``turn_jsonl`` and whose ``created_at`` is older than the
    TTL (default 30 days), removing the matching ``.gz`` files from disk too.
    Rows with kind ``evidence_bundle`` / ``transcript`` (or any other derived
    kind) are NEVER pruned — those are the durable audit trail.

Both functions no-op (return 0) when the ``run_artifacts`` table is absent or
the run dir / DB doesn't exist. The DB is gated with the same PRAGMA-table
introspection used by :mod:`mini_ork.dispatch.telemetry`, so old DBs keep
working without the migration.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import sqlite3
import time
from pathlib import Path

DEFAULT_TTL_DAYS = 30
PRUNABLE_KINDS = ("turn_jsonl",)
PRESERVED_KINDS = ("evidence_bundle", "transcript")


def _open_db(db_path: str | Path) -> sqlite3.Connection | None:
    db = Path(db_path)
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect(str(db), timeout=5)
    except sqlite3.Error:
        return None
    try:
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "run_artifacts" not in tables:
            con.close()
            return None
    except sqlite3.Error:
        con.close()
        return None
    return con


def gzip_run_stream(run_dir: str | Path) -> int:
    """Gzip every ``agent-*.stream.jsonl`` under ``run_dir`` to a matching
    ``.stream.jsonl.gz`` and rewrite the run_artifacts row. Returns the number
    of files gzipped. Idempotent: a pre-existing ``.gz`` is skipped."""
    rd = Path(run_dir)
    if not rd.is_dir():
        return 0
    # rel_path is a bare basename shared across runs; the UPDATE below MUST be
    # scoped by run_id or one run's gzip clobbers other runs' same-basename
    # rows. The run dir is named after the run_id (run-<epoch>-<pid>).
    run_id = os.environ.get("MINI_ORK_RUN_ID") or rd.name
    if not run_id:
        return 0
    matches = sorted(rd.glob("agent-*.stream.jsonl"))
    if not matches:
        return 0
    con = _open_db(os.environ.get("MINI_ORK_DB", ""))
    try:
        now = int(time.time())
        gzipped = 0
        for src in matches:
            gz = src.with_suffix(src.suffix + ".gz")
            if gz.exists() and gz.stat().st_mtime >= src.stat().st_mtime:
                continue
            try:
                with open(src, "rb") as fin, gzip.open(gz, "wb", compresslevel=6) as fout:
                    while True:
                        chunk = fin.read(65536)
                        if not chunk:
                            break
                        fout.write(chunk)
            except OSError:
                continue
            try:
                size = gz.stat().st_size
                h = hashlib.sha256()
                with open(gz, "rb") as fh:
                    for chunk in iter(lambda: fh.read(65536), b""):
                        h.update(chunk)
                digest = h.hexdigest()
            except OSError:
                continue
            rel_gz = gz.name
            if con is not None:
                try:
                    con.execute(
                        "UPDATE run_artifacts "
                        "SET rel_path=?, bytes=?, sha256=?, created_at=? "
                        "WHERE rel_path=? AND run_id=?",
                        (rel_gz, size, digest, now, src.name, run_id),
                    )
                except sqlite3.Error:
                    pass
            gzipped += 1
        if con is not None:
            con.commit()
        return gzipped
    finally:
        if con is not None:
            con.close()


def prune_from_env(db_path: str | Path | None = None) -> int:
    """Best-effort TTL prune wired into the run lifecycle (roadmap Step 2 / A2).

    ``MO_TRAJECTORY_TTL_DAYS`` overrides the default TTL; 0/negative disables.
    The db path follows the canonical contract (MINI_ORK_DB →
    $MINI_ORK_HOME/state.db → .mini-ork/state.db). Never raises — retention is
    housekeeping, not a run gate.
    """
    try:
        ttl = int(os.environ.get("MO_TRAJECTORY_TTL_DAYS", str(DEFAULT_TTL_DAYS)))
    except ValueError:
        ttl = DEFAULT_TTL_DAYS
    if ttl <= 0:
        return 0
    db = db_path or os.environ.get("MINI_ORK_DB") or os.path.join(
        os.environ.get("MINI_ORK_HOME", ".mini-ork"), "state.db")
    try:
        return prune_old_trajectories(db, ttl_days=ttl)
    except Exception:
        return 0


def prune_old_trajectories(
    db_path: str | Path, *, ttl_days: int = DEFAULT_TTL_DAYS
) -> int:
    """Delete ``turn_jsonl`` rows older than ``ttl_days`` AND their on-disk
    ``.gz`` siblings. Evidence bundles / transcripts (and any other non-
    turn_jsonl kind) are never deleted. Returns the number of rows removed.
    No-op when the table is absent (old DB)."""
    if ttl_days <= 0:
        return 0
    con = _open_db(db_path)
    if con is None:
        return 0
    try:
        existing = {row[1] for row in con.execute("PRAGMA table_info(run_artifacts)")}
        if "rel_path" not in existing or "kind" not in existing or "created_at" not in existing:
            return 0
        cutoff = int(time.time()) - ttl_days * 86400
        placeholders = ",".join("?" for _ in PRUNABLE_KINDS)
        rows = con.execute(
            f"SELECT rel_path, run_id, node_id FROM run_artifacts "
            f"WHERE kind IN ({placeholders}) AND created_at < ?",
            (*PRUNABLE_KINDS, cutoff),
        ).fetchall()
        if not rows:
            return 0
        deleted = 0
        run_dir = os.environ.get("MINI_ORK_RUN_DIR", "")
        for rel_path, run_id, node_id in rows:
            target = Path(run_dir) / rel_path if run_dir else None
            if target is not None:
                try:
                    if target.is_file():
                        target.unlink()
                except OSError:
                    pass
            cur = con.execute(
                "DELETE FROM run_artifacts "
                "WHERE kind=? AND rel_path=? AND run_id=? AND (node_id IS ? OR node_id=?)",
                ("turn_jsonl", rel_path, run_id, node_id, node_id),
            )
            deleted += cur.rowcount
        con.commit()
        return deleted
    finally:
        con.close()