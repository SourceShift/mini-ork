"""cross_epic_gradient — Python port of lib/cross_epic_gradient.sh.

Fan-out gradient transfer across task_classes: when the SAME target recurs
across >=`min_classes` distinct task_classes with confidence >= `min_confidence`
inside a sliding window, promote it to a pseudo task_class `__cross_class__`
so `context_assembler` injects the lesson into every task's failure_modes pane.

Co-existence model (strangler-fig): bash `lib/cross_epic_gradient.sh` is the
authoritative source. This module mirrors its inline-Python heredoc byte-for-byte
(per `lib/cross_epic_gradient.sh` lines 35-120). Parity is enforced by
`tests/unit/test_cross_epic_gradient_py.py` (>=6 cases; the gating case is an
end-to-end LIVE-bash subprocess against a temp DB seeded identically to the
Python side, asserting byte-equal gradient_records rows + identical stdout int).

Idempotence: gradient_id = 'gr-cx-' + sha256(target.encode('utf-8')).hexdigest()[:12].
Mixed bash+python re-runs converge via UPSERT (deterministic id ⇒ same row ⇒
UPDATE, not duplicate INSERT).

Public API:
  promote(min_classes=2, min_confidence=0.7, window='14d', db=None) -> int

`db` resolution: explicit kwarg > $MINI_ORK_DB env > $MINI_ORK_HOME/state.db —
mirrors the bash `STATE_DB` precedence.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

__all__ = ["promote", "_parse_window"]

_WINDOW_RE = re.compile(r"^(\d+)([dh])$")
_DEFAULT_WINDOW_SECS = 14 * 86400


def _parse_window(window: str) -> int:
    """Parse `Nd` (days) or `Nh` (hours) → seconds. Fallback 14d on bad input.

    Mirrors lib/cross_epic_gradient.sh:
        m = re.match(r"^(\\d+)([dh])$", window.strip())
        secs = (int(m.group(1)) * (86400 if m.group(2) == 'd' else 3600)) if m else 14 * 86400
    """
    m = _WINDOW_RE.match(window.strip())
    if not m:
        return _DEFAULT_WINDOW_SECS
    n, unit = int(m.group(1)), m.group(2)
    return n * (86400 if unit == "d" else 3600)


def _find_recurring_targets(
    con: sqlite3.Connection, since: int, min_conf: float, min_classes: int
) -> list[tuple[str, str, int]]:
    """First pass: find targets recurring across >=min_classes distinct classes
    with confidence >=min_conf inside the [since, now] window.

    Mirrors the SQL in lib/cross_epic_gradient.sh (interpolation of `since` is
    unchanged so the parity test surfaces any integer-format drift; `min_conf`
    and `min_classes` use parameter binding to match bash).
    """
    rows = con.execute(
        f"""
        SELECT target,
               GROUP_CONCAT(DISTINCT task_class) AS classes,
               COUNT(DISTINCT task_class)        AS n_classes
          FROM gradient_records
         WHERE created_at >= {since}
           AND task_class NOT IN ('', '__cross_class__')
           AND task_class IS NOT NULL
           AND confidence >= ?
           AND target IS NOT NULL AND target != ''
         GROUP BY target
        HAVING n_classes >= ?
        """,
        (min_conf, min_classes),
    ).fetchall()
    return [(t, c, n) for t, c, n in rows]


def _pick_exemplar(
    con: sqlite3.Connection, target: str
) -> Optional[tuple[float, str, str, str]]:
    """Pick the highest-confidence matching row for `target` (deterministic tie-break
    by sqlite3's natural row order). Mirrors the bash inner query.
    """
    row = con.execute(
        """
        SELECT confidence, signal, suggested_change, evidence
          FROM gradient_records
         WHERE target = ?
           AND task_class NOT IN ('', '__cross_class__')
           AND task_class IS NOT NULL
         ORDER BY confidence DESC
         LIMIT 1
        """,
        (target,),
    ).fetchone()
    return row  # (conf, signal, suggested_change, evidence) | None


def _upsert_cross_class(
    con: sqlite3.Connection, target: str, exemplar: tuple
) -> bool:
    """Insert-or-update the `__cross_class__` row for `target`.

    Returns True iff NEW row (counts toward `promoted`), False iff UPDATE in place.
    Mirrors the bash branch + truncation invariants:
      sig, suggested, evidence are [:500]/[:500]/[:1000]-truncated respectively,
      AND inserted in (gid, cx_target, sig[:500], suggested[:1000], evidence[:1000], ...)
      NOT (sig, suggested, evidence) — that ordering is load-bearing for the
      parity test.
    """
    top_conf, top_signal, top_change, top_evidence = exemplar
    gid = "gr-cx-" + hashlib.sha256(target.encode("utf-8")).hexdigest()[:12]
    cx_target = f"cross_class:{target}"
    sig = top_signal or f"Target {target} flagged across task_classes"
    suggested = top_change or "Lesson fanned out across task_classes"
    evidence = top_evidence or ""

    exists = con.execute(
        "SELECT 1 FROM gradient_records WHERE gradient_id=? LIMIT 1", (gid,)
    ).fetchone()
    if exists:
        con.execute(
            """
            UPDATE gradient_records
               SET confidence=?, suggested_change=?, signal=?,
                   evidence=?, target=?, task_class='__cross_class__'
             WHERE gradient_id=?
            """,
            (top_conf, suggested[:1000], sig[:500], evidence[:1000], cx_target, gid),
        )
        return False
    con.execute(
        """
        INSERT INTO gradient_records
            (gradient_id, target, signal, suggested_change,
             evidence, confidence, task_class, created_at)
        VALUES (?,?,?,?,?,?, '__cross_class__', ?)
        """,
        (
            gid,
            cx_target,
            sig[:500],
            suggested[:1000],
            evidence[:1000],
            top_conf,
            int(time.time()),
        ),
    )
    return True


def _resolve_db(db: Optional[str]) -> str:
    """Mirror bash `STATE_DB="${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db}"`."""
    if db is not None:
        return db
    env_db = os.environ.get("MINI_ORK_DB")
    if env_db:
        return env_db
    home = os.environ.get("MINI_ORK_HOME", ".mini-ork")
    return str(Path(home) / "state.db")


def promote(
    min_classes: int = 2,
    min_confidence: float = 0.7,
    window: str = "14d",
    db: Optional[str] = None,
) -> int:
    """Promote cross-class gradients. Returns count of NEW __cross_class__ rows.

    Mirrors lib/cross_epic_gradient.sh::cross_epic_gradient_promote byte-for-byte.
    `sqlite3.Error` propagates (no silent-0 fallback — bash's heredoc also raises
    via uncaught Python, so the parity holds).
    """
    db_path = _resolve_db(db)
    secs = _parse_window(window)
    since = int(time.time()) - secs

    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        recurring = _find_recurring_targets(con, since, min_confidence, min_classes)
        rows = []
        for target, classes, n_classes in recurring:
            exemplar = _pick_exemplar(con, target)
            if exemplar:
                # (target, classes, n_classes, conf, signal, suggested, evidence)
                rows.append((target, classes, n_classes) + exemplar)
        promoted = 0
        for row in rows:
            target = row[0]
            exemplar = row[3:]
            if not target:
                continue
            if _upsert_cross_class(con, target, exemplar):
                promoted += 1
        con.commit()
    finally:
        con.close()
    print(promoted)
    return promoted
