"""pattern_store — Python port of ``lib/pattern_store.sh``.

Faithful port of the PatternRecord storage, query, and mining CLI in
``lib/pattern_store.sh``. The bash script is the authoritative source —
this module gives Python callers an in-process target and gives
``tests/unit/test_pattern_store_py.py`` a stable surface to byte-diff
against the LIVE bash subprocess (no mocks, no hardcoded outputs).

Co-existence model (strangler-fig): bash ``lib/pattern_store.sh`` stays
byte-identical. The Python port mirrors its CLI semantics exactly,
including the bash-private ``_pattern_ensure_table`` DDL (which differs
slightly from migration 0011's CHECK — see SCHEMA NOTE below). Parity
is enforced by ``tests/unit/test_pattern_store_py.py`` (>=7 live-
subprocess cases that drive ``bash lib/pattern_store.sh <args>`` on
identical inputs and diff rc + stdout + DB row contents).

Public API (1:1 with bash CLI):
  store(payload, *, db_path=None, on_new_hooks=None) -> tuple[str, bool]
      Mirror of ``pattern_store <json_payload>``. ``payload`` is a
      Python dict (already-parsed JSON — the bash CLI takes a JSON
      string and parses it itself; the port narrows the API to a dict
      so callers cannot accidentally pass malformed JSON). Returns
      ``(pattern_id, is_new)`` mirroring the ``pid|new/updated``
      sentinel that bash prints internally and then strips before
      echoing the bare pid on stdout. ``on_new_hooks`` is an optional
      list of zero-arg-or-one-arg callables invoked on a new pattern
      with the (pid, payload) pair, matching bash's per-process
      ``_PATTERN_ON_NEW_HOOKS`` array (errors swallowed via ``|| true``
      to mirror bash).
  query(*, db_path=None, min_frequency=1, output_type="") -> list[dict]
      Mirror of ``pattern_query [--min-frequency N] [--output-type T]``.
      Returns a list of row dicts (sqlite3.Row → dict) sorted by
      frequency DESC. The bash CLI emits a JSON-encoded list on
      stdout; the port returns the parsed list directly. Tests
      JSON-decode bash stdout and diff lists element-wise.
  mine_from_traces(*, db_path=None, window="7d", min_cluster=3) -> int
      Mirror of ``pattern_store_mine_from_traces --window Nd --min-cluster N``.
      Parses ``Nd``/``Nh`` (default ``7d`` → 604800s), queries
      ``execution_traces`` for (task_class, status) clusters whose
      count >= ``min_cluster`` within the window, and upserts one
      ``pattern_records`` row per cluster with deterministic
      ``pat-<sha256[:12]>`` ids. Returns the count of clusters
      upserted (mirrors bash's stdout ``print(written)``).
  on_new_register(callable) -> None
      Mirror of ``pattern_on_new <hook_fn_name>``. Appends to the
      module-global ``_ON_NEW_HOOKS`` registry (intentionally
      cross-call persistent; bash's registry is per-subprocess-fresh
      — this is a documented API divergence). The registry is exposed
      for tests via ``pattern_store._ON_NEW_HOOKS.clear()``.

DB path resolution mirrors bash:
    ${MINI_ORK_DB:-${MINI_ORK_HOME:-$(pwd)/.mini-ork}/state.db}

Per-process schema-init guard mirrors bash's ``_MO_PATTERN_SCHEMA_INIT``
env var: a module-global bool ensures ``_ensure_table`` runs the
``CREATE TABLE IF NOT EXISTS`` exactly once per process, regardless of
how many ``store()`` / ``mine_from_traces()`` calls follow.

SCHEMA NOTE: The bash's ``_pattern_ensure_table`` DDL declares
``CHECK(output_type IN ('adr','verifier_addition','workflow_change',
'prompt_change','best_practice_rule','other'))`` — six values
including ``'other'``. Migration 0011 in db/migrations declares a
five-value CHECK that OMITS ``'other'``. When the parity test
initialises a fresh DB via ``db/init.sh``, the 5-tuple CHECK wins
(because ``CREATE TABLE IF NOT EXISTS`` is a no-op against an already-
migrated table). Inserts with ``output_type='other'`` therefore FAIL
on both sides equivalently — the parity test asserts the rejection,
not the storage of 'other'. This is a known schema drift in bash; the
port faithfully mirrors it (the same coercion runs, the same insert
fails).

TIMESTAMP NOTE: All ``first_seen`` / ``last_seen`` writes use SQLite's
``strftime('%Y-%m-%dT%H:%M:%fZ','now')`` inside the INSERT/UPDATE
SQL, matching bash byte-for-byte. Python's ``strftime('%f')`` produces
6-digit microseconds while SQLite's ``%f`` produces 3-digit fractional
seconds — using SQLite avoids the format drift entirely. The
WHERE-clause comparison in ``mine_from_traces`` uses
``strftime('%Y-%m-%dT%H:%M:%S.000Z', <since_epoch>, 'unixepoch')``
which mirrors bash's
``datetime.datetime.utcfromtimestamp(since_epoch).strftime('%Y-%m-%dT%H:%M:%S.000Z')``
byte-for-byte.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from typing import Callable

__all__ = [
    "store",
    "query",
    "mine_from_traces",
    "on_new_register",
    "_db_path",
    "_ensure_table",
    "_ON_NEW_HOOKS",
]

# Mirrors the bash-side valid_types set in lib/pattern_store.sh::pattern_store.
# When output_type is not in this set, the port coerces to 'other' — matching
# bash. Note: 'other' is NOT in migration 0011's CHECK constraint, so against
# a fully-migrated DB both sides will reject the coerced value equivalently
# (sqlite3.IntegrityError). See SCHEMA NOTE in the module docstring.
_VALID_TYPES = frozenset({
    "adr",
    "verifier_addition",
    "workflow_change",
    "prompt_change",
    "best_practice_rule",
    "other",
})

# Module-global registry of on_new hooks. Mirrors bash's
# _PATTERN_ON_NEW_HOOKS=() array but persists across calls in the same
# process (bash's array is per-subprocess-fresh — documented API divergence).
_ON_NEW_HOOKS: list[Callable] = []

# Per-process schema-init guard. Mirrors bash's _MO_PATTERN_SCHEMA_INIT env
# var; once set to True, _ensure_table returns immediately without issuing
# another CREATE TABLE statement.
_SCHEMA_INITIALIZED: bool = False


def _db_path(db_path: str | None = None) -> str:
    """Resolve the SQLite DB path. Mirrors bash's `${MINI_ORK_DB:-...}` chain.

    Args:
        db_path: explicit override (Python-only escape hatch). When
            supplied, returned verbatim.

    Returns:
        The SQLite database file path. Falls back identically to bash:
        ``${MINI_ORK_DB:-${MINI_ORK_HOME:-$(pwd)/.mini-ork}/state.db}``.
    """
    if db_path:
        return db_path
    return os.environ.get("MINI_ORK_DB") or os.path.join(
        os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork"),
        "state.db",
    )


def _ensure_table(db_path: str) -> None:
    """Mirror bash ``_pattern_ensure_table`` — DDL guard for pattern_records.

    Bash uses an env-var guard (``_MO_PATTERN_SCHEMA_INIT=1``) that's
    exported once per subprocess. The port uses a module-global bool —
    one CREATE TABLE per process, regardless of how many store/mine calls.

    The DDL body mirrors bash verbatim (including the 6-tuple CHECK that
    includes 'other'). When run against a DB already migrated by 0011,
    this is a no-op because the table exists; the 5-tuple CHECK from
    migration 0011 then applies to subsequent inserts.
    """
    global _SCHEMA_INITIALIZED
    if _SCHEMA_INITIALIZED:
        return
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS pattern_records (
                pattern_id         TEXT PRIMARY KEY,
                description        TEXT NOT NULL,
                evidence_trace_ids TEXT NOT NULL DEFAULT '[]',
                frequency          INTEGER NOT NULL DEFAULT 1,
                first_seen         TEXT NOT NULL,
                last_seen          TEXT NOT NULL,
                output_type        TEXT NOT NULL
                                   CHECK(output_type IN (
                                       'adr','verifier_addition','workflow_change',
                                       'prompt_change','best_practice_rule','other'
                                   )),
                cluster_id         TEXT
            )
            """
        )
        con.commit()
    finally:
        con.close()
    _SCHEMA_INITIALIZED = True


def _coerce_evidence(raw) -> list:
    """Mirror bash's ``new_evidence`` coercion: dict/list as-is; str → json.loads → [].

    bash:
        new_evidence = p.get("evidence_trace_ids", [])
        if isinstance(new_evidence, str):
            try:
                new_evidence = json.loads(new_evidence)
            except Exception:
                new_evidence = []
    """
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []
    if isinstance(raw, list):
        return raw
    return []


def _coerce_output_type(raw: str) -> str:
    """Mirror bash's ``if output_type not in valid_types: output_type = 'other'``."""
    if raw in _VALID_TYPES:
        return raw
    return "other"


def store(
    payload: dict,
    *,
    db_path: str | None = None,
    on_new_hooks: list[Callable] | None = None,
) -> tuple[str, bool]:
    """Mirror bash ``pattern_store <json_payload>`` — upsert a PatternRecord.

    Args:
        payload: dict (already-parsed). Keys read: pattern_id,
            description, evidence_trace_ids, frequency, output_type.
            Unspecified keys default to ``""`` / ``[]`` / ``1`` / ``"other"``
            — mirrors bash's ``p.get(...)`` defaults.
        db_path: explicit DB override (Python-only).
        on_new_hooks: optional list of callables fired on is_new with
            (pid, payload). Errors are swallowed to mirror bash's
            ``|| true`` semantics. When omitted, the module-global
            ``_ON_NEW_HOOKS`` registry is used.

    Returns:
        ``(pattern_id, is_new)`` — mirrors the bash sentinel
        ``pid|new/updated`` (stripped by the wrapper). is_new is True
        iff no row existed for the id prior to this call.

    Raises:
        KeyError: when ``MINI_ORK_DB`` (and the MINI_ORK_HOME fallback
            chain) resolves to an unset/empty string — mirrors bash's
            ``${MINI_ORK_DB:?MINI_ORK_DB unset}``.
        sqlite3.IntegrityError: when the DB's CHECK constraint rejects
            the coerced output_type (e.g. migration 0011 rejects
            ``'other'``). Mirrors bash, where the same insert raises
            uncaught and the wrapper returns an empty pid.
    """
    db = _db_path(db_path)
    if not db:
        raise KeyError("MINI_ORK_DB unset")

    _ensure_table(db)

    pid = payload.get("pattern_id") or f"pat-{uuid.uuid4().hex[:12]}"
    output_type = _coerce_output_type(payload.get("output_type", "other"))
    new_evidence = _coerce_evidence(payload.get("evidence_trace_ids", []))
    description = payload.get("description", "")
    frequency_initial = int(payload.get("frequency", 1))

    # SQLite-side strftime('%Y-%m-%dT%H:%M:%fZ','now') matches bash's
    # strftime byte-for-byte (3-digit fractional seconds). Using Python
    # strftime('%f') would produce 6-digit microseconds → drift.
    now_sql = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"

    con = sqlite3.connect(db)
    try:
        existing = con.execute(
            "SELECT frequency, evidence_trace_ids, first_seen "
            "FROM pattern_records WHERE pattern_id=?",
            (pid,),
        ).fetchone()
        is_new = existing is None

        if is_new:
            con.execute(
                f"""
                INSERT INTO pattern_records
                    (pattern_id, description, evidence_trace_ids, frequency,
                     first_seen, last_seen, output_type)
                VALUES (?,?,?,?,{now_sql},{now_sql},?)
                """,
                (
                    pid,
                    description,
                    json.dumps(new_evidence),
                    frequency_initial,
                    output_type,
                ),
            )
        else:
            freq = existing[0] + 1
            old_ev_raw = existing[1] if existing[1] else "[]"
            try:
                old_ev = json.loads(old_ev_raw)
                if not isinstance(old_ev, list):
                    old_ev = []
            except (ValueError, TypeError):
                old_ev = []
            merged_ev = list(dict.fromkeys(old_ev + new_evidence))
            con.execute(
                f"""
                UPDATE pattern_records
                SET frequency=?, evidence_trace_ids=?,
                    last_seen={now_sql}, output_type=?
                WHERE pattern_id=?
                """,
                (
                    freq,
                    json.dumps(merged_ev),
                    output_type,
                    pid,
                ),
            )
        con.commit()
    finally:
        con.close()

    if is_new:
        hooks = on_new_hooks if on_new_hooks is not None else _ON_NEW_HOOKS
        for hook in hooks:
            try:
                hook(pid, payload)
            except Exception:
                # Mirror bash's `|| true` — a misbehaving hook does not
                # fail the store. Failures are intentionally NOT logged
                # here to keep row-diff parity deterministic; the
                # caller can register their own logging wrapper.
                pass

    return (pid, is_new)


def query(
    *,
    db_path: str | None = None,
    min_frequency: int = 1,
    output_type: str = "",
) -> list[dict]:
    """Mirror bash ``pattern_query [--min-frequency N] [--output-type T]``.

    Returns a list of row dicts (sqlite3.Row → dict) sorted by frequency
    DESC. Empty string ``output_type`` skips that filter (matches bash's
    ``if ot:`` guard). The bash CLI emits ``json.dumps([...])`` on
    stdout; the port returns the parsed list directly.
    """
    db = _db_path(db_path)
    if not db:
        raise KeyError("MINI_ORK_DB unset")

    clauses = ["frequency >= ?"]
    params: list = [min_frequency]
    if output_type:
        clauses.append("output_type = ?")
        params.append(output_type)
    sql = (
        "SELECT * FROM pattern_records WHERE "
        + " AND ".join(clauses)
        + " ORDER BY frequency DESC"
    )

    con = sqlite3.connect(db)
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def mine_from_traces(
    *,
    db_path: str | None = None,
    window: str = "7d",
    min_cluster: int = 3,
) -> int:
    """Mirror bash ``pattern_store_mine_from_traces --window Nd --min-cluster N``.

    Args:
        db_path: explicit DB override.
        window: duration string of form ``Nd`` (days) or ``Nh`` (hours).
            Default ``7d`` → 604800s. Unparseable window falls back to
            7d (matches bash's ``if not m: secs = 7 * 86400`` branch).
        min_cluster: minimum traces per cluster (default 3, matches
            bash). Clusters with count < min_cluster are skipped.

    Returns:
        int — count of (task_class, status) clusters upserted into
        pattern_records. Mirrors bash's ``print(written)``.

    Determinism: each cluster's pattern_id is ``pat-<sha256(task_class|status)[:12]>``
    so re-mining upserts in place rather than duplicating rows.
    """
    db = _db_path(db_path)
    if not db:
        raise KeyError("MINI_ORK_DB unset")

    _ensure_table(db)

    m = re.match(r"^(\d+)([dh])$", window.strip())
    if not m:
        secs = 7 * 86400
    else:
        n = int(m.group(1))
        unit = m.group(2)
        secs = n * (86400 if unit == "d" else 3600)
    since_epoch = int(time.time()) - secs
    # SQLite-side strftime mirrors bash's
    # datetime.utcfromtimestamp(since_epoch).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    # byte-for-byte (no fractional seconds; always .000Z).
    since_iso_sql = "strftime('%Y-%m-%dT%H:%M:%S.000Z', ?, 'unixepoch')"

    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        con.row_factory = sqlite3.Row
        clusters = con.execute(
            f"""
            SELECT task_class, status, COUNT(*) AS freq,
                   GROUP_CONCAT(trace_id, ',') AS trace_ids
              FROM execution_traces
             WHERE created_at >= {since_iso_sql}
               AND task_class IS NOT NULL AND task_class <> ''
               AND status IS NOT NULL AND status <> ''
             GROUP BY task_class, status
            HAVING COUNT(*) >= ?
            """,
            (since_epoch, min_cluster),
        ).fetchall()

        now_sql = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
        written = 0
        for c in clusters:
            task_class = c["task_class"]
            status = c["status"]
            freq = c["freq"]
            trace_csv = c["trace_ids"] or ""
            trace_ids = [t for t in trace_csv.split(",") if t]
            key = f"{task_class}|{status}".encode()
            pid = "pat-" + hashlib.sha256(key).hexdigest()[:12]
            desc = (
                f"cluster: task_class={task_class} "
                f"status={status} (freq={freq} in window)"
            )
            output_type = (
                "verifier_addition"
                if status in ("failure", "vacuous")
                else "best_practice_rule"
            )
            existing = con.execute(
                "SELECT frequency, evidence_trace_ids "
                "FROM pattern_records WHERE pattern_id=?",
                (pid,),
            ).fetchone()
            if existing:
                old_ev_raw = existing["evidence_trace_ids"] or "[]"
                try:
                    old_ev = json.loads(old_ev_raw)
                    if not isinstance(old_ev, list):
                        old_ev = []
                except (ValueError, TypeError):
                    old_ev = []
                merged = list(dict.fromkeys(old_ev + trace_ids))[:200]
                con.execute(
                    f"""
                    UPDATE pattern_records
                       SET frequency=?, evidence_trace_ids=?,
                           last_seen={now_sql}, description=?, output_type=?
                     WHERE pattern_id=?
                    """,
                    (
                        freq,
                        json.dumps(merged),
                        desc,
                        output_type,
                        pid,
                    ),
                )
            else:
                con.execute(
                    f"""
                    INSERT INTO pattern_records
                        (pattern_id, description, evidence_trace_ids, frequency,
                         first_seen, last_seen, output_type)
                    VALUES (?,?,?,?,{now_sql},{now_sql},?)
                    """,
                    (
                        pid,
                        desc,
                        json.dumps(trace_ids[:200]),
                        freq,
                        output_type,
                    ),
                )
            written += 1
        con.commit()
    finally:
        con.close()
    return written


def on_new_register(callable_: Callable) -> None:
    """Mirror bash ``pattern_on_new <hook_fn_name>``.

    Appends to the module-global ``_ON_NEW_HOOKS`` registry. Errors are
    intentionally NOT raised — accepting any callable matches bash's
    lenient name-only registration. Tests reset the registry between
    cases via ``pattern_store._ON_NEW_HOOKS.clear()``.

    NOTE: the bash ``pattern_on_new`` emits a stderr info line
    ("pattern_on_new: registered hook '...'") for every registration.
    The Python port is silent — Python callers use exceptions for
    errors, not stderr noise. This API divergence is intentional and
    is NOT tested for parity.
    """
    _ON_NEW_HOOKS.append(callable_)