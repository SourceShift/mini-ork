"""Reads and writes of the four ``rsi_*`` tables. No policy lives here.

The ledger's whole purpose is to make the *next* cycle cheaper than this one.
Two queries carry that weight:

* ``candidate_by_signature`` — "have we already merged or already failed at
  exactly this gap?" The queue asks it before spending anything.
* ``reconcile`` — "which open candidates did this harvest fail to reproduce?"
  That is the only thing that stops the queue spinning on a gap someone fixed
  by hand, or marking a merged gap open again because a stale probe still
  reports it.

Everything that decides *what to do* about a row belongs in ``queue.py`` or
``cycle.py``; this module decides nothing.

Schema ownership: ``db/migrations/0084_rsi_cycles.sql`` is the only place the
tables are defined. ``require_tables`` refuses to work on a half-migrated DB
rather than creating tables on the fly — a silent self-bootstrap is how two
databases end up with different columns and nobody notices for a month.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Mapping

from .candidates import Candidate, DOMAINS, KIND_BASE

REQUIRED_TABLES: tuple[str, ...] = (
    "rsi_cycles",
    "rsi_candidates",
    "rsi_probe_runs",
    "rsi_credit",
)

CYCLE_STATUSES: tuple[str, ...] = (
    "running",
    "merged",
    "quarantined",
    "rejected",
    "no_candidate",
    "halted",
    "error",
)

CANDIDATE_STATUSES: tuple[str, ...] = (
    "open",
    "claimed",
    "merged",
    "quarantined",
    "rejected",
    "fixed_externally",
)

BUSY_TIMEOUT_MS = 5000

#: Columns ``finish_cycle`` may write. An allowlist, not ``**kwargs`` into SQL,
#: so a caller cannot inject a column name.
_CYCLE_FINISH_COLUMNS: tuple[str, ...] = (
    "status",
    "halt_reason",
    "lease_token",
    "harvest_ok",
    "probes_run",
    "probes_error",
    "candidates_found",
    "candidates_queued",
    "domain",
    "candidate_id",
    "signature",
    "source",
    "lens",
    "suggest_agreement",
    "child_run_id",
    "gates_json",
    "critic_json",
    "metric_before",
    "metric_after",
    "baseline_json",
    "cost_usd",
    "merged_sha",
    "quarantine_reason",
    "quarantine_path",
)


class LedgerUnavailable(RuntimeError):
    """The ``rsi_*`` tables are missing — run ``mini-ork update``."""


def connect(db_path: str) -> sqlite3.Connection:
    """Open the state DB for the driver.

    ``busy_timeout`` is set on every connection because the driver and the
    child run write to the same file; without it a transient lock surfaces as
    ``database is locked`` in the middle of a merge.
    """
    con = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_MS / 1000.0)
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return con


def tables_present(con: sqlite3.Connection) -> bool:
    names = {
        row[0]
        for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    return all(t in names for t in REQUIRED_TABLES)


def require_tables(con: sqlite3.Connection) -> None:
    if tables_present(con):
        return
    names = {
        row[0]
        for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = [t for t in REQUIRED_TABLES if t not in names]
    raise LedgerUnavailable(
        "rsi ledger tables missing (" + ", ".join(missing) + "); run `mini-ork update`"
    )


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


# ── cycles ──────────────────────────────────────────────────────────────────


def start_cycle(
    con: sqlite3.Connection,
    *,
    cycle_id: str,
    now: int,
    mode: str,
    dry_run: bool,
) -> None:
    con.execute(
        "INSERT INTO rsi_cycles (cycle_id, started_at, mode, status, dry_run)"
        " VALUES (?, ?, ?, 'running', ?)",
        (cycle_id, int(now), mode, 1 if dry_run else 0),
    )
    con.commit()


def finish_cycle(
    con: sqlite3.Connection,
    cycle_id: str,
    *,
    now: int,
    status: str,
    **fields: Any,
) -> None:
    if status not in CYCLE_STATUSES:
        raise ValueError(f"unknown cycle status: {status!r}")
    unknown = set(fields) - set(_CYCLE_FINISH_COLUMNS)
    if unknown:
        raise ValueError(f"unknown rsi_cycles columns: {sorted(unknown)}")
    assignments = ["finished_at = ?", "status = ?"]
    values: list[Any] = [int(now), status]
    for key, value in fields.items():
        assignments.append(f"{key} = ?")
        values.append(_coerce(value))
    values.append(cycle_id)
    con.execute(
        f"UPDATE rsi_cycles SET {', '.join(assignments)} WHERE cycle_id = ?",
        values,
    )
    con.commit()


def get_cycle(con: sqlite3.Connection, cycle_id: str) -> dict[str, Any] | None:
    return _row(
        con.execute("SELECT * FROM rsi_cycles WHERE cycle_id = ?", (cycle_id,)).fetchone()
    )


def list_cycles(
    con: sqlite3.Connection, *, status: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    if status is None:
        rows = con.execute(
            "SELECT * FROM rsi_cycles ORDER BY started_at DESC LIMIT ?", (int(limit),)
        ).fetchall()
    else:
        if status not in CYCLE_STATUSES:
            raise ValueError(f"unknown cycle status: {status!r}")
        rows = con.execute(
            "SELECT * FROM rsi_cycles WHERE status = ? ORDER BY started_at DESC LIMIT ?",
            (status, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


# ── candidates ──────────────────────────────────────────────────────────────


def upsert_candidate(con: sqlite3.Connection, cand: Candidate, *, now: int) -> str:
    """Record an observation. Returns ``"new"`` or ``"seen"``.

    ``updated_at`` is bumped only while the row is ``open``. For a closed row it
    records when the outcome was decided, which is what the queue's
    failed-signature TTL measures — "we tried this 3 days ago" must not reset
    every time the harvest sees the gap again, or the TTL never expires and the
    gap is never retried.
    """
    cursor = con.execute(
        "UPDATE rsi_candidates SET"
        "  severity = ?, title = ?, files_json = ?, evidence_json = ?,"
        "  success_metric = ?, success_command = ?, last_seen_at = ?,"
        "  times_seen = times_seen + 1,"
        "  updated_at = CASE WHEN status = 'open' THEN ? ELSE updated_at END"
        " WHERE candidate_id = ?",
        (
            cand.severity,
            cand.title,
            json.dumps(list(cand.files)),
            json.dumps(cand.evidence, sort_keys=True),
            cand.success_metric,
            cand.success_command,
            int(now),
            int(now),
            cand.candidate_id,
        ),
    )
    if cursor.rowcount:
        con.commit()
        return "seen"
    con.execute(
        "INSERT INTO rsi_candidates ("
        "  candidate_id, signature, domain, source, kind, severity, blast_radius,"
        "  files_json, evidence_key, evidence_json, title, success_metric,"
        "  success_command, first_seen_at, last_seen_at, updated_at, times_seen,"
        "  status"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')",
        (
            cand.candidate_id,
            cand.signature,
            cand.domain,
            cand.source,
            cand.kind,
            cand.severity,
            cand.blast_radius,
            json.dumps(list(cand.files)),
            cand.evidence_key,
            json.dumps(cand.evidence, sort_keys=True),
            cand.title,
            cand.success_metric,
            cand.success_command,
            int(cand.first_seen_at),
            int(now),
            int(now),
            int(cand.times_seen),
        ),
    )
    con.commit()
    return "new"


def get_candidate(con: sqlite3.Connection, candidate_id: str) -> dict[str, Any] | None:
    return _row(
        con.execute(
            "SELECT * FROM rsi_candidates WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()
    )


def candidate_by_signature(con: sqlite3.Connection, signature: str) -> dict[str, Any] | None:
    """Most recently updated row for a signature — the avoidance lookup."""
    return _row(
        con.execute(
            "SELECT * FROM rsi_candidates WHERE signature = ?"
            " ORDER BY updated_at DESC LIMIT 1",
            (signature,),
        ).fetchone()
    )


def list_candidates(
    con: sqlite3.Connection,
    *,
    status: str | None = None,
    domain: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if status is not None:
        if status not in CANDIDATE_STATUSES:
            raise ValueError(f"unknown candidate status: {status!r}")
        where.append("status = ?")
        params.append(status)
    if domain is not None:
        if domain not in DOMAINS:
            raise ValueError(f"unknown domain: {domain!r}")
        where.append("domain = ?")
        params.append(domain)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(int(limit))
    rows = con.execute(
        f"SELECT * FROM rsi_candidates{clause} ORDER BY severity DESC, last_seen_at DESC"
        " LIMIT ?",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def set_candidate_status(
    con: sqlite3.Connection,
    candidate_id: str,
    status: str,
    *,
    now: int,
    reason: str = "",
    merged_sha: str | None = None,
) -> None:
    if status not in CANDIDATE_STATUSES:
        raise ValueError(f"unknown candidate status: {status!r}")
    con.execute(
        "UPDATE rsi_candidates SET status = ?, outcome_reason = ?, merged_sha = ?,"
        " updated_at = ? WHERE candidate_id = ?",
        (status, reason, merged_sha, int(now), candidate_id),
    )
    con.commit()


def reconcile(con: sqlite3.Connection, observed: Iterable[str], *,
              now: int, blind_sources: Iterable[str] = ()) -> int:
    """Mark open candidates the harvest did not reproduce as ``fixed_externally``.

    Returns the number of rows closed. A candidate can disappear because the gap
    was fixed by a cycle elsewhere, by a human, or because the probe that
    produced it changed shape — all three mean "stop queueing it and say so",
    which is why the reason names the harvest rather than a cause it cannot see.

    ``blind_sources`` names the producers that could **not** look this time
    (``db:V8`` for a rule whose table is absent, ``probe:tests`` for one that
    errored). Absence from a blind producer is not evidence of absence, so its
    candidates stay open. Only they are protected — a board-wide veto would let
    one permanently-unreadable rule freeze reconciliation forever, which is what
    a home missing a single old table would otherwise get.
    """
    ids = sorted({str(i) for i in observed})
    blind = sorted({str(s) for s in blind_sources})
    where = ["status = 'open'"]
    params: list[Any] = [int(now)]
    if ids:
        where.append(f"candidate_id NOT IN ({','.join('?' for _ in ids)})")
        params.extend(ids)
    if blind:
        where.append(f"source NOT IN ({','.join('?' for _ in blind)})")
        params.extend(blind)
    cursor = con.execute(
        "UPDATE rsi_candidates SET status = 'fixed_externally',"
        " outcome_reason = 'not reproduced by harvest', updated_at = ?"
        f" WHERE {' AND '.join(where)}",
        params,
    )
    con.commit()
    return cursor.rowcount or 0


# ── probes ──────────────────────────────────────────────────────────────────


def record_probe_run(
    con: sqlite3.Connection,
    *,
    cycle_id: str,
    now: int,
    probe_id: str,
    cmd: str,
    status: str,
    rc: int | None,
    duration_ms: int | None,
    findings_count: int = 0,
    payload: Mapping[str, Any] | None = None,
    stderr_digest: str = "",
) -> None:
    con.execute(
        "INSERT OR REPLACE INTO rsi_probe_runs ("
        "  cycle_id, probe_id, cmd, status, rc, duration_ms, findings_count,"
        "  payload_json, stderr_digest, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            cycle_id,
            probe_id,
            cmd,
            status,
            rc,
            duration_ms,
            int(findings_count),
            json.dumps(dict(payload or {}), sort_keys=True),
            stderr_digest,
            int(now),
        ),
    )
    con.commit()


def list_probe_runs(con: sqlite3.Connection, cycle_id: str) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT * FROM rsi_probe_runs WHERE cycle_id = ? ORDER BY probe_id", (cycle_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# ── credit ──────────────────────────────────────────────────────────────────


def record_credit(
    con: sqlite3.Connection,
    *,
    cycle_id: str,
    now: int,
    candidate_id: str | None,
    source: str,
    lens: str | None,
    domain: str,
    outcome: str,
    severity: int = 0,
    utility: float = 0.0,
    cost_usd: float = 0.0,
) -> None:
    con.execute(
        "INSERT INTO rsi_credit ("
        "  cycle_id, candidate_id, source, lens, domain, outcome, severity,"
        "  utility, cost_usd, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            cycle_id,
            candidate_id,
            source,
            lens,
            domain,
            outcome,
            int(severity),
            float(utility),
            float(cost_usd),
            int(now),
        ),
    )
    con.commit()


def credit_stats(con: sqlite3.Connection, *, min_n: int = 3) -> dict[tuple[str, str], dict[str, Any]]:
    """Merge rate by ``(source, lens)``.

    Only groups with at least ``min_n`` observations are returned; the queue
    uses the returned map to reorder, so an empty map means "no evidence yet,
    order by severity" rather than "everything is equally bad".
    """
    rows = con.execute(
        "SELECT source, COALESCE(lens, '') AS lens, COUNT(*) AS n,"
        "       SUM(CASE WHEN outcome = 'merged' THEN 1 ELSE 0 END) AS merged"
        " FROM rsi_credit GROUP BY source, lens HAVING COUNT(*) >= ?",
        (int(min_n),),
    ).fetchall()
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        n = int(row["n"])
        merged = int(row["merged"] or 0)
        out[(row["source"], row["lens"])] = {
            "n": n,
            "merged": merged,
            "rate": merged / n if n else 0.0,
        }
    return out


def _coerce(value: Any) -> Any:
    """JSON-encode dicts/lists for TEXT columns, leave scalars alone."""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True)
    if isinstance(value, bool):
        return 1 if value else 0
    return value


def validate_candidate_row(raw: Mapping[str, Any]) -> None:
    """Cheap consistency check used by ``doctor`` and the ledger tests."""
    if raw["domain"] not in DOMAINS:
        raise ValueError(f"unknown domain in ledger row: {raw['domain']!r}")
    if raw["kind"] not in KIND_BASE:
        raise ValueError(f"unknown kind in ledger row: {raw['kind']!r}")
    if raw["status"] not in CANDIDATE_STATUSES:
        raise ValueError(f"unknown status in ledger row: {raw['status']!r}")
