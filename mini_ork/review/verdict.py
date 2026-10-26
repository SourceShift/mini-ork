"""Verdict policy for the review runtime (sqlite persistence seam).

Extracted from ``mini_ork/pre_push_review.py`` (parity port of
``lib/pre_push_review.sh`` lines 459-522).
"""
from __future__ import annotations

import sqlite3

from mini_ork.review.common import _resolve_db

# ─────────────────────────────────────────────────────────────────────────
# Verdict policy
# ─────────────────────────────────────────────────────────────────────────

_LIFECYCLE_TABLES = ("review_findings", "review_finding_links")

# Excludes issues whose finding was dismissed or is pre-existing, and issues
# whose link itself carries the pre_existing flag.  The ``id`` column is
# pre_push_review_issues.id in every query it is appended to.
_EXCLUSION = (
    " AND id NOT IN ("
    "SELECT l.issue_id FROM review_finding_links l "
    "LEFT JOIN review_findings f ON f.fingerprint = l.fingerprint "
    "WHERE l.pre_existing = 1 "
    "   OR f.status IN ('dismissed','pre_existing'))"
)


def _exclusion(con: sqlite3.Connection) -> str:
    """The lifecycle exclusion clause, or ``""`` when the tables are missing.

    A DB that predates rev6 R2b (or a test that drops the tables) keeps
    today's counts exactly.
    """
    names = {
        row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?)",
            _LIFECYCLE_TABLES,
        ).fetchall()
    }
    if not set(_LIFECYCLE_TABLES) <= names:
        return ""
    return _EXCLUSION


# Mirrors lib/pre_push_review.sh:459-522 (compute_verdict / verdict UPDATE).
def compute_verdict(rid: int, target_branch: str, *, db: str | None = None) -> tuple[str, str]:
    """Mirror the bash verdict-policy heredoc at lines 459-522.

    Returns ``(verdict, rationale)`` where verdict ∈
    ``{approve, warn, block}``. The rationale format is
    ``"target=<t> crit=<c> high=<h> blocking=<b> (heuristic=<hh>, consensus=<ch>) total=<n>"``
    — identical to the bash UPDATE.

    Args:
        rid: review id to compute verdict for.
        target_branch: target branch name (``main`` / ``master`` / feature).
        db: explicit state.db path; defaults to :func:`_resolve_db`.
    """
    if db is None:
        db = _resolve_db()
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        excl = _exclusion(con)
        cur = con.execute(
            "SELECT COUNT(*) FROM pre_push_review_issues "
            "WHERE review_id=? AND severity='critical' AND status='open'"
            + excl,
            (rid,),
        ).fetchone()[0]
        critical = int(cur)
        cur = con.execute(
            "SELECT COUNT(*) FROM pre_push_review_issues "
            "WHERE review_id=? AND severity='high' AND status='open'"
            + excl,
            (rid,),
        ).fetchone()[0]
        high = int(cur)
        cur = con.execute(
            "SELECT COUNT(*) FROM pre_push_review_issues "
            "WHERE review_id=? AND status='open'"
            + excl,
            (rid,),
        ).fetchone()[0]
        total = int(cur)
        cur = con.execute(
            "SELECT COUNT(*) FROM pre_push_review_issues "
            "WHERE review_id=? AND severity='high' AND status='open' "
            "AND lens LIKE 'heuristic.%'"
            + excl,
            (rid,),
        ).fetchone()[0]
        heuristic_high = int(cur)
        cur = con.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT file_path FROM pre_push_review_issues "
            "  WHERE review_id=? AND severity='high' AND status='open' "
            "    AND lens LIKE 'llm.%' AND file_path IS NOT NULL"
            + excl + " "
            "  GROUP BY file_path HAVING COUNT(DISTINCT lens) >= 2"
            ")",
            (rid,),
        ).fetchone()[0]
        consensus_high = int(cur)
    finally:
        con.close()

    blocking_high = heuristic_high + consensus_high
    to_main = target_branch in ("main", "master")
    if critical > 0:
        verdict = "block"
    elif blocking_high > 0:
        verdict = "block" if to_main else "warn"
    elif high > 0 or total > 5:
        verdict = "warn"
    else:
        verdict = "approve"

    rationale = (
        f"target={target_branch} crit={critical} high={high} "
        f"blocking={blocking_high} (heuristic={heuristic_high}, "
        f"consensus={consensus_high}) total={total}"
    )
    return verdict, rationale


def _apply_verdict(rid: int, target_branch: str, db: str) -> None:
    """Compute + persist verdict for ``rid``. Mirrors the bash UPDATE."""
    verdict, rationale = compute_verdict(rid, target_branch, db=db)
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        excl = _exclusion(con)
        cur = con.execute(
            "SELECT COUNT(*) FROM pre_push_review_issues "
            "WHERE review_id=? AND status='open'"
            + excl,
            (rid,),
        ).fetchone()[0]
        issues_open = int(cur)
        cur = con.execute(
            "SELECT COUNT(*) FROM pre_push_review_issues "
            "WHERE review_id=? AND severity='critical' AND status='open'"
            + excl,
            (rid,),
        ).fetchone()[0]
        issues_critical = int(cur)
        con.execute(
            "UPDATE pre_push_reviews "
            "   SET verdict=?, issues_open=?, issues_critical=?, rationale=? "
            " WHERE id=?",
            (verdict, issues_open, issues_critical, rationale, rid),
        )
        con.commit()
    finally:
        con.close()
