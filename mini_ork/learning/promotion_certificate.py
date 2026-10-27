"""Anytime-valid promotion certificates for the apply gate.

The apply loop is a stream of gate tests. A fixed-sample p-value at one
constant level has no memory of earlier tests, so the stream's error rate grows
with its length. This module supplies the two pieces the gate needs instead:

* a **test-by-betting e-value** over the paired probe outcomes,
  ``E = (1 + bet) ** gains * (1 - bet) ** losses``. Under H0 every factor
  ``1 + bet * (after - before)`` has conditional expectation at most 1, so E is
  a test supermartingale and Ville's inequality bounds
  ``P(sup E >= 1 / alpha) <= alpha`` under any stopping rule.
* a **per-task-class alpha ledger**: the k-th test with at least one discordant
  pair gets ``alpha_k = alpha / (k * (k + 1))``. Since ``sum 1/(k(k+1)) = 1``,
  a union bound holds over the class's whole history.

The module is pure math plus one table. The only env reads are inside
``resolve_config``; ``certify`` takes its configuration explicitly.
"""

from __future__ import annotations

import os
import sqlite3

FLAG = "MO_APPLY_CERTIFICATE"
ALPHA_ENV = "MO_APPLY_CERT_ALPHA"
BET_ENV = "MO_APPLY_CERT_BET"
LEDGER_ENV = "MO_APPLY_CERT_LEDGER"

DEFAULT_ALPHA = 0.05
DEFAULT_BET = 0.75
LEDGER_MODES = ("spend", "none")

# The CREATE TABLE / CREATE INDEX statements from db/migrations/
# 0086_promotion_certificates.sql, byte-for-byte. The migration's
# schema_migrations insert is intentionally not included: it belongs to the
# migration, not to a table the runtime may need to create on a DB that
# predates it.
DDL = """CREATE TABLE IF NOT EXISTS promotion_certificates (
  id            INTEGER PRIMARY KEY,
  task_class    TEXT    NOT NULL,
  slot          INTEGER NOT NULL CHECK (slot >= 1),
  candidate_id  TEXT    NOT NULL,
  gains         INTEGER NOT NULL CHECK (gains >= 0),
  losses        INTEGER NOT NULL CHECK (losses >= 0),
  n             INTEGER NOT NULL CHECK (n >= 0),
  bet           REAL    NOT NULL,
  e_value       REAL    NOT NULL,
  ledger        TEXT    NOT NULL CHECK (ledger IN ('spend','none')),
  alpha_total   REAL    NOT NULL,
  alpha_k       REAL    NOT NULL,
  threshold     REAL    NOT NULL,
  certified     INTEGER NOT NULL CHECK (certified IN (0,1)),
  created_at    INTEGER NOT NULL,
  UNIQUE (task_class, slot)
);
CREATE INDEX IF NOT EXISTS idx_promotion_certificates_class ON promotion_certificates(task_class, slot);
"""


def ensure_table(con: sqlite3.Connection) -> None:
    """Create the certificate table/index if they are missing.

    Idempotent, so it also covers a DB that predates migration 0086.
    """
    con.executescript(DDL)


def normalize_task_class(task_class: str) -> str:
    """Canonical per-class key: trimmed, hyphens folded to underscores."""
    normalized = task_class.strip().replace("-", "_")
    if not normalized:
        raise ValueError("task_class must be non-empty")
    return normalized


def paired_counts(before: list, after: list) -> tuple[int, int]:
    """Return ``(gains, losses)`` over paired binary outcomes.

    A gain is ``after[i]`` truthy where ``before[i]`` was falsy; a loss is the
    reverse. Ties are counted in neither.
    """
    if len(before) != len(after):
        raise ValueError("before and after must have the same length")
    gains = sum(1 for b, a in zip(before, after) if a and not b)
    losses = sum(1 for b, a in zip(before, after) if b and not a)
    return gains, losses


def betting_e_value(gains: int, losses: int, bet: float) -> float:
    """``(1 + bet) ** gains * (1 - bet) ** losses`` — a test supermartingale."""
    if gains < 0:
        raise ValueError("gains must be >= 0")
    if losses < 0:
        raise ValueError("losses must be >= 0")
    if not 0.0 < bet < 1.0:
        raise ValueError("bet must be in (0, 1)")
    return (1.0 + bet) ** gains * (1.0 - bet) ** losses


def spend_fraction(k: int) -> float:
    """The k-th test's share of the total budget: ``1 / (k * (k + 1))``."""
    if k < 1:
        raise ValueError("k must be >= 1")
    return 1.0 / (k * (k + 1))


def slot_alpha(alpha_total: float, k: int, ledger: str) -> float:
    """The alpha the k-th test is allowed to spend under ``ledger``."""
    if ledger == "spend":
        return alpha_total * spend_fraction(k)
    if ledger == "none":
        return alpha_total
    raise ValueError(f"unknown ledger {ledger!r}")


def _parse_unit_interval(environ: dict, key: str, default: float,
                         errors: list[str]) -> float:
    raw = environ.get(key)
    if not raw:
        return default
    try:
        parsed = float(raw)
        if not 0.0 < parsed < 1.0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append(f"{key}={raw!r} must be a number in (0, 1)")
        return default
    return parsed


def resolve_config(environ: dict | None = None) -> dict:
    """Resolve the certificate env contract.

    Returns ``{"enabled", "alpha", "bet", "ledger", "errors"}``. An unset or
    empty value takes its default. An invalid value takes its default AND
    appends one error string prefixed with the offending env var's name.
    """
    if environ is None:
        environ = os.environ
    errors: list[str] = []
    alpha = _parse_unit_interval(environ, ALPHA_ENV, DEFAULT_ALPHA, errors)
    bet = _parse_unit_interval(environ, BET_ENV, DEFAULT_BET, errors)

    ledger = "spend"
    raw_ledger = environ.get(LEDGER_ENV)
    if raw_ledger:
        if raw_ledger in LEDGER_MODES:
            ledger = raw_ledger
        else:
            errors.append(
                f"{LEDGER_ENV}={raw_ledger!r} must be one of "
                f"{', '.join(LEDGER_MODES)}")

    return {
        "enabled": environ.get(FLAG) == "1",
        "alpha": alpha,
        "bet": bet,
        "ledger": ledger,
        "errors": errors,
    }


def certify(con: sqlite3.Connection, *, task_class: str, candidate_id: str,
            before: list, after: list, alpha_total: float, bet: float,
            ledger: str, now: int) -> dict:
    """Score one gate test against the class's alpha ledger.

    A test with no discordant pair consumes no slot and writes no row: its
    e-value is exactly 1, so leaving it out preserves
    ``E[e * 1{counted}] <= P(counted)``. Every test with at least one
    discordant pair consumes a slot, whatever branch decided it — an e-value
    below 1 is evidence, not a reason to drop the test.
    """
    tc = normalize_task_class(task_class)
    gains, losses = paired_counts(before, after)
    e_value = betting_e_value(gains, losses, bet)

    base = {
        "task_class": tc,
        "candidate_id": candidate_id,
        "gains": gains,
        "losses": losses,
        "n": len(before),
        "bet": bet,
        "ledger": ledger,
        "alpha_total": alpha_total,
        "e_value": e_value,
    }

    if gains + losses == 0:
        return {
            **base,
            "consumed": False,
            "slot": None,
            "alpha_k": None,
            "threshold": None,
            "certified": False,
        }

    ensure_table(con)
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT COALESCE(MAX(slot), 0) + 1 FROM promotion_certificates "
            "WHERE task_class = ?", (tc,)).fetchone()
        slot = int(row[0])
        alpha_k = slot_alpha(alpha_total, slot, ledger)
        threshold = 1.0 / alpha_k
        certified = e_value >= threshold
        con.execute(
            "INSERT INTO promotion_certificates (task_class, slot, "
            "candidate_id, gains, losses, n, bet, e_value, ledger, "
            "alpha_total, alpha_k, threshold, certified, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tc, slot, candidate_id, gains, losses, len(before), bet,
             e_value, ledger, alpha_total, alpha_k, threshold,
             1 if certified else 0, now))
        con.commit()
    except Exception:
        con.rollback()
        raise

    return {
        **base,
        "consumed": True,
        "slot": slot,
        "alpha_k": alpha_k,
        "threshold": threshold,
        "certified": certified,
    }
