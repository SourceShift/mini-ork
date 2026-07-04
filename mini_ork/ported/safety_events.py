"""safety_events — Python port of lib/safety_events.sh.

Faithful port of the bash CLI in ``lib/safety_events.sh`` that writes
and queries safety incident records in ``state.db:safety_events``. The
bash script is the authoritative source — this module gives Python
callers an in-process target and gives
``tests/unit/test_safety_events_py.py`` a stable surface to byte-diff
against the LIVE bash subprocess (no mocks, no hardcoded outputs).

Co-existence model (strangler-fig): bash ``lib/safety_events.sh`` stays
byte-identical. The Python port mirrors its CLI semantics exactly.
Parity is enforced by ``tests/unit/test_safety_events_py.py`` (>=6
live-subprocess cases that drive ``bash lib/safety_events.sh <args>`` on
identical inputs and diff rc + stdout + DB row contents).

Pipeline map (bash CLI → Python function):
  mo_safety_event_emit <tripwire> <severity> <evidence> [run_id] [recipe]
                            → emit(tripwire_id, severity, evidence_json,
                                   run_id='', recipe='', db=None) -> dict
                               Returns {"id": str, "rc": int}.
                               rc=0 success, rc=2 missing/invalid severity,
                               rc=3 invalid JSON, rc=0 + warn stderr when
                               table absent (no-op).
  mo_safety_event_list_open [tripwire]
                            → list_open(tripwire_id='', db=None) -> list[dict]
                               JSONL-per-line dicts with `evidence` key
                               (parses evidence_json with raw-string
                               fallback on parse failure — mirrors bash).
  mo_safety_event_acknowledge <event_id> <operator_response>
                            → acknowledge(event_id, operator_response,
                                            db=None) -> dict
                               Returns {"rc": int, "updated": int}.
                               rc=2 missing args; rc=0 + warn stderr when
                               table absent (no-op). Updates only rows with
                               status='open' (mirror bash WHERE clause).
  mo_safety_event_resolve <event_id> <resolution_note>
                            → resolve(event_id, resolution_note,
                                       db=None) -> dict
                               Returns {"rc": int, "updated": int}.
                               Updates only rows with status IN ('open',
                               'acknowledged'). Sets resolution_ts via
                               int(time.time()) mirroring bash's
                               strftime('%s','now') (UTC epoch seconds).

DB path resolution mirrors bash ``_mo_se_db``:
    ${MINI_ORK_DB:-${MINI_ORK_HOME:-$(pwd)/.mini-ork}/state.db}
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import sys
import time
from datetime import datetime, timezone

__all__ = [
    "emit",
    "list_open",
    "acknowledge",
    "resolve",
    "_db_path",
    "_table_exists",
    "_validate_severity",
    "_validate_json",
    "_new_id",
    "_now_epoch",
    "_log",
]

_SEVERITIES = ("critical", "high", "medium", "low")


def _now_epoch() -> int:
    """Mirror bash ``strftime('%s','now')`` — UTC epoch seconds at call time.

    Bash uses ``(strftime('%s','now'))`` (the default CURRENT_TIMESTAMP for
    the ts column) and `int(time.time())` here in the same wall-clock
    window — both produce the same integer within ±1s drift. The parity
    test strips ``ts`` before comparing rows / list_open JSONL precisely
    so this drift cannot break parity.
    """
    return int(time.time())


def _now_iso() -> str:
    """Mirror bash ``date -u +%Y-%m-%dT%H:%M:%SZ`` for stderr log lines.

    Truncated to seconds so the log line matches bash byte-for-byte.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id() -> str:
    """Mirror bash ``secrets.token_hex(16)`` — 32 lowercase hex chars.

    bash line 50-52: ``python3 -c "import secrets; print(secrets.token_hex(16))"``.
    We call the same function in-process. Parity tests assert id SHAPE
    (length + hex pattern) not id VALUE because both sides generate a
    fresh id per emit.
    """
    return secrets.token_hex(16)


def _db_path(db: str | None = None) -> str:
    """Resolve the SQLite DB path. Mirrors bash ``_mo_se_db``.

    Args:
        db: explicit override (Python-only escape hatch; bash always uses
            env vars). When supplied, returned verbatim.

    Falls back identically to bash:
        ${MINI_ORK_DB:-${MINI_ORK_HOME:-$(pwd)/.mini-ork}/state.db}
    """
    if db:
        return db
    return os.environ.get("MINI_ORK_DB") or os.path.join(
        os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork"),
        "state.db",
    )


def _log(level: str, msg: str) -> None:
    """Emit a structured stderr log line matching bash ``_mo_se_log``.

    bash format:
        {"level":"<level>","subsystem":"safety_events","ts":"<iso>","msg":"<msg>"}

    One line per call, written to stderr so callers can redirect
    (e.g. ``2>err.log``) without losing the JSON envelope shape. The
    parity test inspects stderr lines for the "warn" + "table absent"
    phrase to confirm the table-missing no-op branch.
    """
    line = (
        f'{{"level":"{level}",'
        f'"subsystem":"safety_events",'
        f'"ts":"{_now_iso()}",'
        f'"msg":"{msg}"}}'
    )
    print(line, file=sys.stderr)


def _table_exists(db_path: str) -> bool:
    """Mirror bash ``_mo_se_table_exists`` — true when safety_events table present.

    bash line 30-35:
        sqlite3 "$_db" "SELECT 1 FROM sqlite_master WHERE type='table'
                          AND name='safety_events'" | grep -q '^1$'

    Returns False when the DB file itself is missing (bash guards with
    ``[ -f "$_db" ] || return 1``). The Python port does the same — a
    fresh path with no DB file yet is treated as "table absent", which
    is what bash's table-missing no-op branch handles.
    """
    if not os.path.isfile(db_path):
        return False
    try:
        con = sqlite3.connect(db_path)
        try:
            cur = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='safety_events'"
            )
            return cur.fetchone() is not None
        finally:
            con.close()
    except sqlite3.Error:
        return False


def _validate_severity(severity: str) -> bool:
    """Mirror bash ``_mo_se_validate_severity`` — enum check.

    bash line 37-42: ``critical|high|medium|low``. Returns False for
    anything else (including empty string).
    """
    return severity in _SEVERITIES


def _validate_json(payload: str) -> bool:
    """Mirror bash ``_mo_se_validate_json`` — strict JSON parse.

    bash line 44-48:
        [ -z "$_payload" ] && return 0
        python3 -c "import json,sys; json.loads(sys.argv[1])" "$_payload"

    Empty payload returns True (bash short-circuits before the json.loads
    call — this lets ``mo_safety_event_emit TW-1 high ''`` succeed and
    default to ``{}``). Non-empty payload must round-trip through
    ``json.loads`` without raising.
    """
    if not payload:
        return True
    try:
        json.loads(payload)
    except (ValueError, TypeError):
        return False
    return True


def _parse_evidence(evidence_json: str) -> object:
    """Mirror bash list_open ``evidence`` key — parsed JSON or raw string fallback.

    bash line 140-143:
        try:
            out["evidence"] = json.loads(out.pop("evidence_json"))
        except Exception:
            out["evidence"] = out.pop("evidence_json")

    Returns the parsed object on success, the original string on
    parse failure. Tests assert that an unparseable evidence_json (e.g.
    a plain non-JSON string) round-trips through list_open with the
    string preserved verbatim.
    """
    try:
        return json.loads(evidence_json)
    except (ValueError, TypeError):
        return evidence_json


def emit(
    tripwire_id: str,
    severity: str,
    evidence_json: str,
    run_id: str = "",
    recipe: str = "",
    *,
    db: str | None = None,
) -> dict:
    """Mirror ``mo_safety_event_emit`` — write one safety event row.

    Required positional arguments:
        tripwire_id   — non-empty (else rc=2)
        severity      — must be one of critical|high|medium|low (else rc=2)
        evidence_json — must round-trip json.loads (empty OK; rc=3 on bad JSON)

    Optional arguments:
        run_id        — when non-empty, gates the 60s idempotency window
        recipe        — recorded on the row verbatim (None when empty)
        db            — explicit DB path override (Python-only)

    Returns ``{"id": str, "rc": int}``:
        rc=0 success (id is the 32-hex id of the new or deduped row)
        rc=2 missing tripwire_id or invalid severity
        rc=3 invalid evidence_json

    Table-missing no-op branch mirrors bash: when the safety_events
    table is absent (e.g. db/init.sh hasn't applied 0036_safety_events.sql
    yet), the function emits a warn line to stderr and returns rc=0
    with id="" — same shape bash emits.
    """
    if not tripwire_id or not severity:
        _log("error",
             "mo_safety_event_emit <tripwire_id> <severity> <evidence_json> [run_id] [recipe]")
        return {"id": "", "rc": 2}
    if not _validate_severity(severity):
        _log("error",
             f"invalid severity: {severity} (must be critical|high|medium|low)")
        return {"id": "", "rc": 2}
    if not _validate_json(evidence_json):
        _log("error", "evidence_json failed JSON validation")
        return {"id": "", "rc": 3}

    db_path = _db_path(db)
    if not _table_exists(db_path):
        _log("warn", "safety_events table absent; emit is a no-op. Run migrations.")
        return {"id": "", "rc": 0}

    if not evidence_json:
        evidence_json = "{}"

    con = sqlite3.connect(db_path)
    try:
        if run_id:
            cur = con.execute(
                """
                SELECT id FROM safety_events
                WHERE tripwire_id = ?
                  AND run_id = ?
                  AND ts >= (strftime('%s','now') - 60)
                ORDER BY ts DESC LIMIT 1
                """,
                (tripwire_id, run_id),
            )
            row = cur.fetchone()
            if row:
                existing_id = row[0]
                _log("info",
                     f"emitted safety_event id={existing_id} tripwire={tripwire_id} "
                     f"severity={severity} run={run_id}")
                return {"id": existing_id, "rc": 0}

        new_id = _new_id()
        con.execute(
            """
            INSERT INTO safety_events
                (id, tripwire_id, severity, run_id, recipe, evidence_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (new_id, tripwire_id, severity,
             run_id if run_id else None,
             recipe if recipe else None,
             evidence_json),
        )
        con.commit()
    finally:
        con.close()

    _log("info",
         f"emitted safety_event id={new_id} tripwire={tripwire_id} "
         f"severity={severity} run={run_id}")
    return {"id": new_id, "rc": 0}


def list_open(tripwire_id: str = "", *, db: str | None = None) -> list[dict]:
    """Mirror ``mo_safety_event_list_open`` — return rows where status='open'.

    Args:
        tripwire_id: when non-empty, filters ``WHERE tripwire_id=?``.
        db:         explicit DB path override (Python-only).

    Returns a list of dicts (one per row) matching bash's JSONL output:
        {"id", "ts", "tripwire_id", "severity", "run_id", "recipe",
         "status", "evidence": <parsed or raw string>}

    The ``evidence`` key replaces bash's evidence_json column with the
    parsed JSON value (raw string on parse failure — mirrors bash's
    fallback). Tests strip ``ts`` before diffing because both sides
    compute it at call time with ±1s drift.

    Table-missing branch mirrors bash: warns on stderr, returns ``[]``
    with rc equivalent = 0 (function returns the empty list, not an rc).
    """
    db_path = _db_path(db)
    if not _table_exists(db_path):
        _log("warn", "safety_events table absent; nothing to list")
        return []

    where = "status='open'"
    params: tuple = ()
    if tripwire_id:
        where += " AND tripwire_id=?"
        params = (tripwire_id,)

    con = sqlite3.connect(db_path)
    try:
        con.row_factory = sqlite3.Row
        cur = con.execute(
            f"""
            SELECT id, ts, tripwire_id, severity, run_id, recipe,
                   evidence_json, status
            FROM safety_events WHERE {where} ORDER BY ts DESC
            """,
            params,
        )
        rows = cur.fetchall()
    finally:
        con.close()

    out: list[dict] = []
    for row in rows:
        d = dict(row)
        d["evidence"] = _parse_evidence(d.pop("evidence_json"))
        out.append(d)
    return out


def acknowledge(
    event_id: str,
    operator_response: str,
    *,
    db: str | None = None,
) -> dict:
    """Mirror ``mo_safety_event_acknowledge`` — open → acknowledged.

    Required positional arguments:
        event_id           — non-empty (else rc=2)
        operator_response  — non-empty (else rc=2)

    Optional:
        db — explicit DB path override

    Returns ``{"rc": int, "updated": int}``:
        rc=0 success (updated = rowcount, 0 when no matching row in
                       status='open'; 1 when transitioning one row)
        rc=2 missing args

    The UPDATE only matches ``status='open'`` (bash line 166-168); rows
    in any other state are left untouched. Table-missing no-op branch
    mirrors bash: warn stderr, rc=0, updated=0.
    """
    if not event_id or not operator_response:
        _log("error",
             "mo_safety_event_acknowledge <event_id> <operator_response>")
        return {"rc": 2, "updated": 0}

    db_path = _db_path(db)
    if not _table_exists(db_path):
        _log("warn", "safety_events table absent; ack is a no-op")
        return {"rc": 0, "updated": 0}

    con = sqlite3.connect(db_path)
    try:
        cur = con.execute(
            "UPDATE safety_events SET status='acknowledged', operator_response=? "
            "WHERE id=? AND status='open'",
            (operator_response, event_id),
        )
        con.commit()
        updated = cur.rowcount
    finally:
        con.close()

    _log("info", f"acknowledged safety_event id={event_id}")
    return {"rc": 0, "updated": updated}


def resolve(
    event_id: str,
    resolution_note: str,
    *,
    db: str | None = None,
) -> dict:
    """Mirror ``mo_safety_event_resolve`` — open|acknowledged → resolved.

    Required positional arguments:
        event_id         — non-empty (else rc=2)
        resolution_note  — non-empty (else rc=2)

    Optional:
        db — explicit DB path override

    Returns ``{"rc": int, "updated": int}``:
        rc=0 success (updated = rowcount of rows that transitioned)
        rc=2 missing args

    The UPDATE matches ``status IN ('open','acknowledged')`` (bash line
    193-195) and sets ``resolution_ts = int(time.time())`` (UTC epoch
    seconds — mirrors bash's `int(time.time())` Python heredoc). Table-
    missing no-op branch: warn stderr, rc=0, updated=0.
    """
    if not event_id or not resolution_note:
        _log("error",
             "mo_safety_event_resolve <event_id> <resolution_note>")
        return {"rc": 2, "updated": 0}

    db_path = _db_path(db)
    if not _table_exists(db_path):
        _log("warn", "safety_events table absent; resolve is a no-op")
        return {"rc": 0, "updated": 0}

    con = sqlite3.connect(db_path)
    try:
        cur = con.execute(
            "UPDATE safety_events SET status='resolved', resolution_ts=?, resolution_note=? "
            "WHERE id=? AND status IN ('open','acknowledged')",
            (int(time.time()), resolution_note, event_id),
        )
        con.commit()
        updated = cur.rowcount
    finally:
        con.close()

    _log("info", f"resolved safety_event id={event_id}")
    return {"rc": 0, "updated": updated}