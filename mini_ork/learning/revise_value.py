"""CoBa: measure the marginal value of another revise round.

The revise loop (``mini_ork/cli/execute.py``) grants an implementer round when
a verifier fails, up to ``MO_REVISE_ROUNDS``. That cap is blind to history: it
spends the last round exactly like the first. CoBa (arXiv 2608.07424) allocates
a fixed budget between generating another attempt and stopping by the measured
marginal value of each. This module is the measurement half:

  record_round      -- a round was granted (idempotent on run/target/round)
  settle_round      -- the source that fired it reported pass (1) or fail (0)
  next_round_value  -- P(pass at ``round_no`` | settled history), plus n
  should_stop       -- cheap guard: enough samples AND too little value

The rule only fires with real history: a fresh home has no rows, so every run
keeps the old behaviour exactly. The environment can only make stopping
*rarer* -- ``MO_COBA_MIN_SAMPLES`` clamps up to 20 and ``MO_COBA_MIN_GAIN``
clamps down to 0.05 -- and ``MO_COBA=0`` disables the rule outright.

Every function swallows DB errors and returns a neutral value: the revise loop
is on the critical path of a run, and a history write that crashes it is worse
than no history at all. ``record_round`` also creates the table when absent so
a state.db that never ran migration 0081 still works (the migration and
``_SCHEMA`` must not drift apart).
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Any

#: Default and floor for the settled-sample count. ``MO_COBA_MIN_SAMPLES`` may
#: only raise it: fewer samples than this is not evidence.
DEFAULT_MIN_SAMPLES = 20
#: Default and ceiling for the next-round pass-rate threshold. ``MO_COBA_MIN_GAIN``
#: may only lower it, so the environment can only make stopping rarer.
DEFAULT_MIN_GAIN = 0.05

_NEUTRAL_VALUE: dict[str, Any] = {"p": None, "n": 0}

#: Runtime copy of db/migrations/0081_revise_rounds.sql's CREATE TABLE. Kept in
#: sync with the migration by hand; the migration is authoritative.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS revise_rounds (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT    NOT NULL,
    task_class  TEXT,
    target_node TEXT    NOT NULL,
    round       INTEGER NOT NULL,
    passed      INTEGER,
    cost_usd    REAL,
    created_at  INTEGER NOT NULL,
    UNIQUE (run_id, target_node, round)
)
"""


def _connect(db_path: str) -> sqlite3.Connection | None:
    """Open ``db_path`` and ensure the table exists.

    ``None`` when there is no path. Raises ``sqlite3.Error`` / ``OSError`` on an
    unusable path (missing directory, corrupt file) for the caller to swallow.
    """
    if db_path is None or str(db_path).strip() == "":
        return None
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    try:
        conn.execute(_SCHEMA)
        conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _coba_disabled() -> bool:
    raw = os.environ.get("MO_COBA")
    if raw is None:
        return False
    return str(raw).strip().lower() in {"0", "false", "off", "no"}


def record_round(
    db: str,
    run_id: str,
    task_class: str | None,
    target_node: str,
    round_no: int,
    cost_usd: float | None = None,
) -> None:
    """Record that ``target_node`` was granted ``round_no`` in this run.

    Idempotent on the unique ``(run_id, target_node, round)``: a re-record keeps
    whatever ``passed`` the row already carries and never raises.
    """
    try:
        conn = _connect(db)
        if conn is None:
            return
        try:
            conn.execute(
                "INSERT OR IGNORE INTO revise_rounds "
                "(run_id, task_class, target_node, round, cost_usd, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    task_class,
                    target_node,
                    int(round_no),
                    cost_usd,
                    int(time.time()),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 -- history must never crash the revise loop
        return


def settle_round(
    db: str,
    run_id: str,
    target_node: str,
    round_no: int,
    passed: bool,
) -> None:
    """Mark ``round_no`` of this run/target as passed (1) or failed (0)."""
    try:
        conn = _connect(db)
        if conn is None:
            return
        try:
            conn.execute(
                "UPDATE revise_rounds SET passed = ? "
                "WHERE run_id = ? AND target_node = ? AND round = ?",
                (1 if passed else 0, run_id, target_node, int(round_no)),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 -- history must never crash the revise loop
        return


def next_round_value(
    db: str,
    task_class: str | None,
    target_node: str,
    round_no: int,
) -> dict[str, Any]:
    """Pass rate of settled history at ``round_no`` for this class+target.

    ``{"p": None, "n": 0}`` when there is no settled history; otherwise ``p`` is
    the fraction of rows that passed and ``n`` the number of rows counted.
    """
    try:
        conn = _connect(db)
        if conn is None:
            return dict(_NEUTRAL_VALUE)
        try:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(passed), 0) FROM revise_rounds "
                "WHERE task_class IS ? AND target_node = ? AND round = ? "
                "AND passed IS NOT NULL",
                (task_class, target_node, int(round_no)),
            ).fetchone()
        finally:
            conn.close()
        n = int(row[0] or 0) if row is not None else 0
        if n == 0:
            return dict(_NEUTRAL_VALUE)
        return {"p": float(row[1]) / n, "n": n}
    except Exception:  # noqa: BLE001 -- history must never crash the revise loop
        return dict(_NEUTRAL_VALUE)


def should_stop(
    db: str,
    task_class: str | None,
    target_node: str,
    round_no: int,
) -> tuple[bool, str]:
    """Should ``round_no`` be withheld instead of granted?

    True only when the settled history has at least ``MO_COBA_MIN_SAMPLES`` rows
    (default 20, clamped up) and its pass rate is below ``MO_COBA_MIN_GAIN``
    (default 0.05, clamped down). ``MO_COBA=0`` disables the rule.
    """
    if _coba_disabled():
        return False, "coba disabled"
    min_samples = max(_env_int("MO_COBA_MIN_SAMPLES", DEFAULT_MIN_SAMPLES),
                      DEFAULT_MIN_SAMPLES)
    min_gain = min(_env_float("MO_COBA_MIN_GAIN", DEFAULT_MIN_GAIN),
                   DEFAULT_MIN_GAIN)
    value = next_round_value(db, task_class, target_node, round_no)
    n = int(value.get("n") or 0)
    p = value.get("p")
    if n < min_samples:
        return False, (
            f"only {n} settled round-{round_no} samples (need {min_samples})"
        )
    if p is None:
        return False, f"no settled round-{round_no} pass rate"
    if p >= min_gain:
        return False, (
            f"next-round pass rate {p:.3f} >= {min_gain:.2f} (n={n})"
        )
    return True, f"next-round pass rate {p:.3f} < {min_gain:.2f} (n={n})"
