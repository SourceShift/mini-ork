"""Finding lifecycle for the pre-push review runtime (rev6 R2b).

A review issue is one *observation*.  A finding is the durable identity behind
observations across reviews: a stable ``fingerprint`` plus a status
(``open`` / ``fixed`` / ``dismissed`` / ``pre_existing``), so a later push can
say "fixed", "same finding", or "this problem predates the push" — and a human
can dismiss one with a reason.

The old per-review rows in ``pre_push_review_issues`` are never rewritten; the
links table is the only join between an observation and its finding, and the
verdict queries exclude dismissed/pre-existing findings at read time.

Schema citations:
  - ``review_findings``       — db/migrations/0085_review_findings.sql
  - ``review_finding_links``  — db/migrations/0085_review_findings.sql
  - ``review_lens_runs``      — db/migrations/0085_review_findings.sql
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import sqlite3
import sys
import time

STATUSES = ("open", "fixed", "dismissed", "pre_existing")

_SEVERITY_CHECK = (
    "CHECK (severity IN ('info','low','medium','high','critical'))"
)

# DDL kept byte-equal to db/migrations/0085_review_findings.sql's CREATE
# statements so whichever runs first wins with the same schema.
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS review_findings (\n"
    "  fingerprint      TEXT PRIMARY KEY,\n"
    "  lens_kind        TEXT NOT NULL CHECK (lens_kind IN ('heuristic','llm')),\n"
    "  file_path        TEXT,\n"
    "  title            TEXT NOT NULL,\n"
    "  severity         TEXT NOT NULL " + _SEVERITY_CHECK + ",\n"
    "  status           TEXT NOT NULL DEFAULT 'open'\n"
    "                   CHECK (status IN ('open','fixed','dismissed','pre_existing')),\n"
    "  first_review_id  INTEGER NOT NULL,\n"
    "  last_review_id   INTEGER NOT NULL,\n"
    "  last_issue_id    INTEGER,\n"
    "  times_seen       INTEGER NOT NULL DEFAULT 1,\n"
    "  reopened         INTEGER NOT NULL DEFAULT 0,\n"
    "  reason           TEXT,\n"
    "  resolved_by      TEXT,\n"
    "  resolved_at      INTEGER,\n"
    "  created_at       INTEGER NOT NULL,\n"
    "  updated_at       INTEGER NOT NULL\n"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_rf_status_file"
    " ON review_findings(status, file_path)",
    "CREATE TABLE IF NOT EXISTS review_finding_links (\n"
    "  issue_id     INTEGER PRIMARY KEY,\n"
    "  fingerprint  TEXT NOT NULL,\n"
    "  review_id    INTEGER NOT NULL,\n"
    "  pre_existing INTEGER NOT NULL DEFAULT 0\n"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_rfl_review ON review_finding_links(review_id)",
    "CREATE TABLE IF NOT EXISTS review_lens_runs (\n"
    "  id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL, lens TEXT NOT NULL,\n"
    "  status TEXT NOT NULL CHECK (status IN"
    " ('ok','dispatch_failed','unparseable','error')),\n"
    "  issues INTEGER NOT NULL DEFAULT 0, elapsed_ms INTEGER,"
    " created_at INTEGER NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_rlr_review ON review_lens_runs(review_id)",
)

_PRIOR_KEYS = ("fingerprint", "status", "severity", "file_path", "title",
               "lens_kind")

_SEVERITY_RANK = (
    "CASE severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3 "
    "WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END DESC"
)


def ensure_schema(con: sqlite3.Connection) -> None:
    """Create the lifecycle tables/indexes if they do not exist (DDL only)."""
    for statement in _SCHEMA:
        con.execute(statement)


def _norm(text: object) -> str:
    """Lowercase, collapse runs of non-alphanumerics to one space, strip."""
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _lens_kind(lens: object) -> str:
    return "heuristic" if str(lens or "").startswith("heuristic.") else "llm"


def fingerprint(issue: dict) -> str:
    """Stable 16-hex identity for an issue.

    A non-empty ``same_as`` is returned unchanged (the caller has checked that
    the finding exists).  Otherwise heuristics key on their lens name; LLM
    lenses do not, so two lenses reporting the same title share one finding.
    """
    same_as = issue.get("same_as")
    if isinstance(same_as, str) and same_as:
        return same_as
    lens = str(issue.get("lens") or "")
    file = issue.get("file")
    title = _norm(issue.get("title"))
    if lens.startswith("heuristic."):
        raw = f"{lens}|{file}|{title}"
    else:
        raw = f"llm|{file}|{title}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def prior_for(con: sqlite3.Connection, files: list[str],
              limit: int = 30) -> list[dict]:
    """Findings on ``files`` that are still live, most severe first."""
    if not files:
        return []
    placeholders = ",".join("?" for _ in files)
    rows = con.execute(
        "SELECT fingerprint, status, severity, file_path, title, lens_kind "
        "  FROM review_findings "
        f" WHERE file_path IN ({placeholders}) AND status != 'fixed' "
        f" ORDER BY {_SEVERITY_RANK}, updated_at DESC "
        "  LIMIT ?",
        (*[str(f) for f in files], int(limit)),
    ).fetchall()
    return [dict(zip(_PRIOR_KEYS, row)) for row in rows]


def link_issue(con: sqlite3.Connection, issue_id: int, review_id: int,
               issue: dict, now: int) -> str:
    """Link one persisted issue row to its finding, creating it if new.

    Never commits or rolls back: the caller owns the transaction.
    """
    same_as = issue.get("same_as")
    fp = ""
    if isinstance(same_as, str) and same_as:
        if con.execute(
            "SELECT 1 FROM review_findings WHERE fingerprint=?", (same_as,)
        ).fetchone():
            fp = same_as
        else:
            # A reference to nothing is not a reference: drop it so the
            # computed fingerprint is what both the row and the caller see.
            issue.pop("same_as", None)
    if not fp:
        fp = fingerprint(issue)

    pre_existing = issue.get("pre_existing") is True
    row = con.execute(
        "SELECT status FROM review_findings WHERE fingerprint=?", (fp,)
    ).fetchone()
    if row is None:
        status = "pre_existing" if pre_existing else "open"
        con.execute(
            "INSERT INTO review_findings "
            "(fingerprint, lens_kind, file_path, title, severity, status,"
            " first_review_id, last_review_id, last_issue_id, times_seen,"
            " reopened, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)",
            (fp, _lens_kind(issue.get("lens")), issue.get("file"),
             issue.get("title") or "",
             str(issue.get("severity") or "medium"), status,
             int(review_id), int(review_id), int(issue_id), int(now), int(now)),
        )
    else:
        con.execute(
            "UPDATE review_findings "
            "   SET last_review_id=?, last_issue_id=?, severity=?, updated_at=?,"
            "       times_seen = times_seen + 1 "
            " WHERE fingerprint=?",
            (int(review_id), int(issue_id),
             str(issue.get("severity") or "medium"), int(now), fp),
        )
        if row[0] == "fixed":
            con.execute(
                "UPDATE review_findings SET status='open',"
                " reopened = reopened + 1 WHERE fingerprint=?",
                (fp,),
            )

    # One link per observation row: re-linking the same issue replaces it.
    con.execute(
        "INSERT OR REPLACE INTO review_finding_links"
        " (issue_id, fingerprint, review_id, pre_existing) VALUES (?, ?, ?, ?)",
        (int(issue_id), fp, int(review_id), 1 if pre_existing else 0),
    )
    return fp


def settle(con: sqlite3.Connection, review_id: int, prior: list[dict],
           observed: set[str], resolved: set[str], now: int) -> list[str]:
    """Mark prior findings fixed when this review proves them gone.

    A heuristic finding that the deterministic lenses did not report is fixed;
    an LLM finding only when the panel named it in ``resolved``.
    """
    fixed: list[str] = []
    for row in prior or []:
        fp = row.get("fingerprint")
        if not fp or row.get("status") != "open" or fp in observed:
            continue
        if row.get("lens_kind") == "heuristic" or fp in resolved:
            con.execute(
                "UPDATE review_findings SET status='fixed', resolved_by=?,"
                " resolved_at=? WHERE fingerprint=?",
                (f"review:{int(review_id)}", int(now), fp),
            )
            fixed.append(fp)
    return fixed


def resolve(db: str, fp: str, status: str, reason: str, by: str,
            now: int | None = None) -> None:
    """Record a human disposition for one finding."""
    if status not in STATUSES or status == "open":
        raise ValueError(f"invalid status: {status!r}")
    if status in ("dismissed", "pre_existing") and not str(reason or "").strip():
        raise ValueError(f"{status} requires a non-empty reason")
    if now is None:
        now = int(time.time())
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        ensure_schema(con)
        if con.execute(
            "SELECT 1 FROM review_findings WHERE fingerprint=?", (fp,)
        ).fetchone() is None:
            raise ValueError(f"unknown fingerprint: {fp}")
        con.execute(
            "UPDATE review_findings SET status=?, reason=?, resolved_by=?,"
            " resolved_at=?, updated_at=? WHERE fingerprint=?",
            (status, reason, by, int(now), int(now), fp),
        )
        con.commit()
    finally:
        con.close()


def list_findings(db: str, status: str = "open", file: str = "",
                  limit: int = 50) -> list[dict]:
    """List findings, most severe first.  ``status='all'`` means no filter."""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=5000")
        ensure_schema(con)
        sql = ("SELECT fingerprint, lens_kind, file_path, title, severity,"
               " status, first_review_id, last_review_id, last_issue_id,"
               " times_seen, reopened, reason, resolved_by, resolved_at,"
               " created_at, updated_at FROM review_findings")
        clauses: list[str] = []
        params: list[object] = []
        if status and status != "all":
            clauses.append("status = ?")
            params.append(status)
        if file:
            clauses.append("file_path = ?")
            params.append(file)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" ORDER BY {_SEVERITY_RANK}, updated_at DESC LIMIT ?"
        params.append(int(limit))
        return [dict(row) for row in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def _db_path() -> str:
    # Lazy: gate imports pre_push_review, which owns this module's callers.
    from mini_ork.review.gate import resolve_db

    return str(resolve_db(".", os.environ))


def _cmd_findings(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="mini-ork review findings")
    parser.add_argument(
        "--status", default="open",
        choices=("open", "fixed", "dismissed", "pre_existing", "all"),
    )
    parser.add_argument("--file", default="")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--json", action="store_true")
    try:
        ns = parser.parse_args(argv)
    except SystemExit as exc:  # argparse usage errors exit via SystemExit(2)
        return int(exc.code or 0)

    rows = list_findings(_db_path(), status=ns.status, file=ns.file,
                         limit=ns.limit)
    if ns.json:
        print(json.dumps(rows))
    else:
        for row in rows:
            print(f"{row['fingerprint']}  {row['status']}  {row['severity']}  "
                  f"{row['file_path'] or '?'}  {row['title']}"
                  f"  (seen {row['times_seen']}×)")
    return 0


def _cmd_resolve(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="mini-ork review resolve")
    parser.add_argument("fingerprint")
    parser.add_argument(
        "--as", dest="as_", required=True,
        choices=("fixed", "dismissed", "pre_existing"),
    )
    parser.add_argument("--reason", default="")
    try:
        ns = parser.parse_args(argv)
    except SystemExit as exc:  # argparse usage errors exit via SystemExit(2)
        return int(exc.code or 0)

    by = "human:" + getpass.getuser()
    try:
        resolve(_db_path(), ns.fingerprint, ns.as_, ns.reason, by)
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    return 0


_USAGE = """Usage: mini-ork review findings|resolve ...

  findings [--status open|fixed|dismissed|pre_existing|all] [--file PATH]
           [--limit N] [--json]
  resolve <fingerprint> --as fixed|dismissed|pre_existing [--reason TEXT]
"""


def main(argv: list[str]) -> int:
    """``mini-ork review findings`` / ``mini-ork review resolve``."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        sys.stderr.write(_USAGE)
        return 2
    if args[0] == "findings":
        return _cmd_findings(args[1:])
    if args[0] == "resolve":
        return _cmd_resolve(args[1:])
    sys.stderr.write(_USAGE)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
