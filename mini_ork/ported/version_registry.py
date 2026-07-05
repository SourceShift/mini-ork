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

import json
import os
import sqlite3
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
    if not env:
        raise RuntimeError("MINI_ORK_DB unset")
    return env


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
    con.execute(
        """
        INSERT INTO version_registry
            (version_id, kind, name, status, payload, previous_stable_version,
             utility_score, created_at)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(version_id) DO UPDATE SET
            payload=excluded.payload,
            status=CASE WHEN status='quarantined' THEN 'quarantined' ELSE excluded.status END
        """,
        (vid, kind, name, p.get("status", "candidate"), json.dumps(p), prev_vid,
         float(p.get("utility_score", 0.0)), now),
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
    con.commit()
    new_cur = con.execute(
        "SELECT * FROM version_registry WHERE version_id=?", (prev_vid,)
    ).fetchone()
    con.close()
    return json.dumps(dict(new_cur)) if new_cur else "null"


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
