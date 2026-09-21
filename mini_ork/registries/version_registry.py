"""Python port of lib/version_registry.sh — VersionRegistry + rollback for
workflows and agents.

Strangler-fig parity port. Each function mirrors the inline-python sqlite3
block of its bash counterpart byte-for-byte in behaviour:

    version_register        <kind> <payload>            -> version_id (stdout)
    version_get             <kind> <version_id>          -> JSON or "null"
    version_current         <kind> <name>               -> JSON of current stable
    version_rollback        <kind> <name>               -> now-current JSON
    version_quarantine      <kind> <version_id> <reason>
    version_can_promote     <kind> <version_id>          -> "true"|"false"
    version_clear_quarantine <version_id> <approver>

The bash functions print the version_id / JSON on stdout; these return the same
string so the parity test can compare stdout AND the resulting DB rows.

Non-determinism mirrored from bash: register() mints ``v-<kind[:3]>-<uuid12>``
when the payload omits ``version_id``, and created_at/promoted_at/quarantined_at
are ``int(time.time())``. Callers wanting determinism pass ``version_id`` in the
payload (bash honours ``p.get("version_id")``); ``now`` is injectable here purely
for tests — bash has no such hook, so the parity test normalises time columns.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
import uuid

_SCHEMA = """
    CREATE TABLE IF NOT EXISTS version_registry (
        version_id               TEXT PRIMARY KEY,
        kind                     TEXT NOT NULL CHECK(kind IN ('workflow','agent')),
        name                     TEXT NOT NULL,
        status                   TEXT NOT NULL DEFAULT 'candidate'
                                     CHECK(status IN ('candidate','stable','quarantined','retired')),
        payload                  TEXT NOT NULL DEFAULT '{}',
        previous_stable_version  TEXT,
        quarantine_reason        TEXT,
        quarantine_cleared_by    TEXT,
        utility_score            REAL DEFAULT 0.0,
        promoted_at              INTEGER,
        quarantined_at           INTEGER,
        created_at               INTEGER NOT NULL
    )
"""


def _db_path(db: str | None) -> str:
    if db:
        return db
    env = os.environ.get("MINI_ORK_DB")
    if env:
        return env
    # MINI_ORK_HOME points AT the .mini-ork dir itself (not its parent), so the
    # state db is a direct child — the same derivation self_improve.py uses.
    # Without this, `mini-ork rollback` dies on "MINI_ORK_DB unset" in any
    # project that was merely `mini-ork init`'d, which is the normal case.
    home = os.environ.get("MINI_ORK_HOME")
    if home:
        return os.path.join(home, "state.db")
    raise RuntimeError("MINI_ORK_DB unset")


def ensure_table(db: str | None = None) -> None:
    con = sqlite3.connect(_db_path(db))
    con.execute("PRAGMA busy_timeout=5000")
    con.execute(_SCHEMA)
    con.commit()
    con.close()


def register(kind: str, payload: str, db: str | None = None, now: int | None = None) -> str:
    """Mirror version_register. Raises ValueError on bad input (bash exits 1)."""
    ensure_table(db)
    try:
        p = json.loads(payload)
    except json.JSONDecodeError as e:
        raise ValueError(f"version_register: invalid JSON: {e}") from e
    name = p.get("name", "")
    if not name:
        raise ValueError("version_register: payload must include 'name'")
    vid = p.get("version_id") or f"v-{kind[:3]}-{uuid.uuid4().hex[:12]}"
    now = int(time.time()) if now is None else now

    con = sqlite3.connect(_db_path(db))
    prev = con.execute(
        "SELECT version_id FROM version_registry WHERE kind=? AND name=? AND status='stable' "
        "ORDER BY promoted_at DESC LIMIT 1",
        (kind, name),
    ).fetchone()
    prev_vid = prev[0] if prev else None
    status = p.get("status", "candidate")

    # First promotion of this name: the new row would have no predecessor, so
    # rollback() would raise and the name would be silently un-rollback-able.
    # Mint a row for the pre-mutation content instead, so every promoted name
    # has a chain to walk back to. Only callers that pass ``baseline_content``
    # get one — everyone else stores exactly what they stored before.
    #
    # promoted_at is deliberately one second EARLIER than the promote below:
    # register() never demotes the outgoing row, so a name can hold two
    # 'stable' rows at once, and the ORDER BY promoted_at DESC lookups in
    # current()/rollback() can only tell them apart if the times differ.
    baseline = p.get("baseline_content")
    if baseline and prev_vid is None and status == "stable":
        baseline_vid = f"v-{kind[:3]}-base-{hashlib.sha1(name.encode()).hexdigest()[:12]}"
        con.execute(
            "INSERT OR IGNORE INTO version_registry "
            "(version_id, kind, name, status, payload, previous_stable_version, "
            " utility_score, promoted_at, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (baseline_vid, kind, name, "stable",
             json.dumps({"name": name, "baseline": True,
                         "target_path": p.get("target_path"),
                         "content": baseline}),
             None, 0.0, now - 1, now - 1),
        )
        prev_vid = baseline_vid

    con.execute(
        """
        INSERT INTO version_registry
            (version_id, kind, name, status, payload, previous_stable_version,
             utility_score, promoted_at, created_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(version_id) DO UPDATE SET
            payload=excluded.payload,
            promoted_at=COALESCE(excluded.promoted_at, promoted_at),
            status=CASE WHEN status='quarantined' THEN 'quarantined' ELSE excluded.status END
        """,
        (vid, kind, name, status, json.dumps(p), prev_vid,
         float(p.get("utility_score", 0.0)),
         now if status == "stable" else None, now),
    )
    con.commit()
    con.close()
    return vid


def get(kind: str, version_id: str, db: str | None = None) -> str:
    ensure_table(db)
    con = sqlite3.connect(_db_path(db))
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT * FROM version_registry WHERE kind=? AND version_id=?", (kind, version_id)
    ).fetchone()
    con.close()
    return json.dumps(dict(row)) if row else "null"


def current(kind: str, name: str, db: str | None = None) -> str:
    ensure_table(db)
    con = sqlite3.connect(_db_path(db))
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT * FROM version_registry WHERE kind=? AND name=? AND status='stable' "
        "ORDER BY promoted_at DESC LIMIT 1",
        (kind, name),
    ).fetchone()
    con.close()
    return json.dumps(dict(row)) if row else "null"


def _restore_target_file(row: dict) -> None:
    """Write the rolled-back version's content back to its target file.

    The status columns alone are not a rollback: the promoted file is still on
    disk carrying the directive we just retired, so a DB-only rollback reports
    success while the running system keeps executing the change. This is the
    step that makes the registry a backstop rather than a ledger.

    Refusals are loud and non-fatal. A row whose ``target_path`` sits outside
    the active root is skipped, never written: the live rows in a long-lived DB
    accumulate absolute paths into whatever worktree promoted them, and
    restoring one would silently rewrite a different checkout than the one
    being rolled back.
    """
    payload = {}
    try:
        payload = json.loads(row.get("payload") or "{}")
    except json.JSONDecodeError:
        payload = {}
    target_path = payload.get("target_path")
    content = payload.get("content")
    if not target_path or content is None:
        # Pre-D3 rows carry only a rollback_hash. Nothing to write — keep the
        # historical DB-only behaviour, but say so, because the caller's
        # "reverted" verdict is now about DB state alone.
        sys.stderr.write(
            f"version_rollback: {row.get('version_id')} has no stored content "
            f"(pre-content row); DB state rolled back, target file left as is\n")
        return

    root = os.path.realpath(os.environ.get("MINI_ORK_ROOT") or os.getcwd())
    real = os.path.realpath(target_path)
    if real != root and not real.startswith(root + os.sep):
        sys.stderr.write(
            f"version_rollback: REFUSING to write {target_path} — outside the "
            f"active root {root}. This row was promoted by a different "
            f"checkout; restore it there, or re-promote here.\n")
        return
    try:
        with open(real, "w", encoding="utf-8") as fh:
            fh.write(content)
    except OSError as e:
        # Last line of defense, now that no human reviews a promotion: a failed
        # restore must not be swallowed.
        sys.stderr.write(
            f"version_rollback: FAILED to restore {real}: {e}; DB state rolled "
            f"back but the file still holds the retired change\n")
        raise
    sys.stderr.write(f"version_rollback: restored {real} from {row.get('version_id')}\n")


def rollback(kind: str, name: str, db: str | None = None, now: int | None = None) -> str:
    ensure_table(db)
    now = int(time.time()) if now is None else now
    con = sqlite3.connect(_db_path(db))
    con.row_factory = sqlite3.Row
    cur = con.execute(
        "SELECT * FROM version_registry WHERE kind=? AND name=? AND status='stable' "
        "ORDER BY promoted_at DESC LIMIT 1",
        (kind, name),
    ).fetchone()
    if not cur:
        con.close()
        raise ValueError(f"version_rollback: no stable version found for {kind}/{name}")
    prev_vid = cur["previous_stable_version"]
    if not prev_vid:
        con.close()
        raise ValueError(
            f"version_rollback: no previous stable version recorded for {cur['version_id']}"
        )
    con.execute("UPDATE version_registry SET status='retired' WHERE version_id=?",
                (cur["version_id"],))
    con.execute("UPDATE version_registry SET status='stable', promoted_at=? WHERE version_id=?",
                (now, prev_vid))
    new_cur = con.execute(
        "SELECT * FROM version_registry WHERE version_id=?", (prev_vid,)
    ).fetchone()
    new_row = dict(new_cur) if new_cur else None
    con.commit()
    con.close()
    if new_row:
        _restore_target_file(new_row)
    return json.dumps(new_row) if new_row else "null"


def targets_for_paths(paths, db: str | None = None) -> list[dict]:
    """Stable rows whose ``payload.target_path`` is one of ``paths``.

    ``rollback`` is keyed on a row's ``name``, and for an applied prompt
    mutation that name is the target file's path as the promoting run saw it —
    an absolute path, not a recipe or a placeholder. A caller that knows which
    files a run touched can therefore find the rows that belong to that run
    instead of guessing a name, which is what a hardcoded ``"default"`` was
    doing: no live row is named that, so the rollback was a silent no-op that
    still reported success.

    Comparison is on ``realpath`` so a relative path from a run record matches
    the absolute path recorded at promotion time. Baseline rows are excluded:
    they share their target path with the promotion they precede, and they are
    what a rollback walks *to*, so returning one as a rollback subject would
    retire it and leave the name with nothing to fall back on. One row per
    ``(kind, name)`` — the most recently promoted — so a name promoted more than
    once is rolled back once.
    """
    wanted = {os.path.realpath(p) for p in paths if p}
    if not wanted:
        return []
    ensure_table(db)
    con = sqlite3.connect(_db_path(db))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT * FROM version_registry WHERE status='stable' "
            "ORDER BY promoted_at ASC"
        ).fetchall()
    finally:
        con.close()
    latest: dict[tuple[str, str], dict] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            continue
        if payload.get("baseline"):
            continue
        target = payload.get("target_path")
        if target and os.path.realpath(target) in wanted:
            latest[(row["kind"], row["name"])] = dict(row)
    return list(latest.values())


def quarantine(kind: str, version_id: str, reason: str, db: str | None = None,
               now: int | None = None) -> None:
    ensure_table(db)
    now = int(time.time()) if now is None else now
    con = sqlite3.connect(_db_path(db))
    con.execute(
        "UPDATE version_registry SET status='quarantined', quarantine_reason=?, "
        "quarantined_at=? WHERE version_id=? AND kind=?",
        (reason, now, version_id, kind),
    )
    con.commit()
    con.close()


def can_promote(kind: str, version_id: str, db: str | None = None) -> str:
    ensure_table(db)
    con = sqlite3.connect(_db_path(db))
    row = con.execute(
        "SELECT status FROM version_registry WHERE kind=? AND version_id=?", (kind, version_id)
    ).fetchone()
    con.close()
    if row is None:
        return "false"
    return "false" if row[0] == "quarantined" else "true"


def clear_quarantine(version_id: str, approver: str, db: str | None = None) -> None:
    """Mirror version_clear_quarantine. Raises ValueError when nothing updated
    (bash exits 1)."""
    ensure_table(db)
    con = sqlite3.connect(_db_path(db))
    updated = con.execute(
        "UPDATE version_registry SET status='candidate', quarantine_cleared_by=?, "
        "quarantine_reason=NULL WHERE version_id=? AND status='quarantined'",
        (approver, version_id),
    ).rowcount
    con.commit()
    con.close()
    if updated == 0:
        raise ValueError(
            f"version_clear_quarantine: {version_id} not found or not quarantined"
        )
