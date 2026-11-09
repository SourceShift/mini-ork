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

from collections.abc import Mapping

import json
import math
import os
import sqlite3
import time

FLAG = "MO_APPLY_CERTIFICATE"
ALPHA_ENV = "MO_APPLY_CERT_ALPHA"
BET_ENV = "MO_APPLY_CERT_BET"
LEDGER_ENV = "MO_APPLY_CERT_LEDGER"

DEFAULT_ALPHA = 0.05
DEFAULT_BET = 0.75
LEDGER_MODES = ("spend", "none")

# The CREATE statements from db/migrations/0089_promotion_certificates.sql,
# byte-for-byte in substance (a test compares them statement by statement).
# The migration's schema_migrations insert is intentionally not included: it
# belongs to the migration, not to a table the runtime may need to create on a
# DB that predates it.
_TABLE_SQL = """CREATE TABLE IF NOT EXISTS promotion_certificates (
  id              INTEGER PRIMARY KEY,
  task_class      TEXT    NOT NULL,
  task_class_raw  TEXT    NOT NULL DEFAULT '',
  slot            INTEGER NOT NULL CHECK (slot >= 1),
  candidate_id    TEXT    NOT NULL,
  run_id          TEXT,
  scorer          TEXT,
  dry_run         INTEGER NOT NULL DEFAULT 0 CHECK (dry_run IN (0,1)),
  probe_set_hash  TEXT,
  probe_ids       TEXT,
  gains           INTEGER NOT NULL CHECK (gains >= 0),
  losses          INTEGER NOT NULL CHECK (losses >= 0),
  n               INTEGER NOT NULL CHECK (n >= 0),
  bet             REAL    NOT NULL,
  e_value         REAL    NOT NULL,
  ledger          TEXT    NOT NULL CHECK (ledger IN ('spend','none')),
  alpha_total     REAL    NOT NULL,
  alpha_k         REAL    NOT NULL,
  threshold       REAL    NOT NULL,
  status          TEXT    NOT NULL CHECK (status IN
                                  ('certified','uncertified','unreachable')),
  created_at      INTEGER NOT NULL,
  UNIQUE (task_class, slot)
);"""

_NO_UPDATE_SQL = """CREATE TRIGGER IF NOT EXISTS promotion_certificates_no_update
BEFORE UPDATE ON promotion_certificates
BEGIN
  SELECT RAISE(ABORT, 'promotion_certificates is append-only');
END;"""

_NO_DELETE_SQL = """CREATE TRIGGER IF NOT EXISTS promotion_certificates_no_delete
BEFORE DELETE ON promotion_certificates
BEGIN
  SELECT RAISE(ABORT, 'promotion_certificates is append-only');
END;"""

DDL_STATEMENTS = (_TABLE_SQL, _NO_UPDATE_SQL, _NO_DELETE_SQL)

DDL = "\n".join(DDL_STATEMENTS)


def ensure_table(con: sqlite3.Connection) -> None:
    """Create the certificate table, triggers and indexes if they are missing.

    Executed one statement at a time instead of via ``executescript``: that
    convenience issues an implicit COMMIT, which would end the caller's
    explicit transaction mid-``certify`` and let a lock error land as a
    half-applied test. Each statement is ``IF NOT EXISTS``, so re-running is a
    no-op and the helper covers a DB that predates migration 0089.
    """
    for stmt in DDL_STATEMENTS:
        con.execute(stmt)


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


def required_gains(slot: int, alpha_total: float, bet: float,
                   ledger: str) -> int:
    """Clean gains (all wins, no losses) needed to certify at ``slot``.

    The bar at slot k is ``1 / alpha_k``; each clean gain multiplies the
    e-value by ``1 + bet``. Returned exactly (candidates are verified against
    ``betting_e_value`` rather than trusting a log/ceil rounding).

    This is the number a pre-flight check needs: if a class's probe set can
    never produce that many discordant pairs, no amount of a second look will
    certify it, and the run should say so BEFORE it spends probe dollars.
    """
    if not 0.0 < bet < 1.0:
        raise ValueError("bet must be in (0, 1)")
    threshold = 1.0 / slot_alpha(alpha_total, slot, ledger)
    g = max(1, math.ceil(math.log(threshold) / math.log(1.0 + bet)))
    while betting_e_value(g, 0, bet) < threshold:
        g += 1
    while g > 1 and betting_e_value(g - 1, 0, bet) >= threshold:
        g -= 1
    return g


def max_e(n: int, bet: float) -> float:
    """The largest e-value ``n`` discordant pairs can ever produce.

    Under this bet every gain multiplies more than every loss divides, so the
    best split is all gains: ``(1 + bet) ** n``. A threshold above this is
    **unreachable** at size n, whatever the next probes do.
    """
    if n < 0:
        raise ValueError("n must be >= 0")
    if not 0.0 < bet < 1.0:
        raise ValueError("bet must be in (0, 1)")
    return (1.0 + bet) ** n


ALPHA_GUARDRAIL = 0.2
ALPHA_LOOSE_ENV = "MO_APPLY_CERT_ALPHA_ALLOW_LOOSE"
# Escape hatch for harnesses that supply the per-task vectors out of band
# (stubbed scorers, MO_APPLY_PERTASK_JSON replays): the probe-set ceiling they
# are checked against is not the ceiling their vectors came from.
PREFLIGHT_ENV = "MO_APPLY_CERT_PREFLIGHT"
_PREFLIGHT_OFF = frozenset({"0", "off", "false", "no"})


def preflight_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the arm-time pre-flight runs (on unless explicitly off)."""
    env = os.environ if environ is None else environ
    return (env.get(PREFLIGHT_ENV) or "").strip().lower() not in _PREFLIGHT_OFF


def ledger_warning(ledger: str) -> str | None:
    """Why a non-default ledger is a weaker guarantee (None for ``spend``).

    ``spend`` is the shipped mode because its ``1/(k(k+1))`` schedule is what
    makes the union bound hold across a class's whole test history. ``none``
    exists for operators who accept that cost, and every consumer that reports
    a certificate should say so rather than silently using it.
    """
    if ledger == "spend":
        return None
    return (
        f"certificate ledger={ledger!r}: every test gets the whole alpha, so "
        "the class-wide union bound no longer holds (each new test re-spends "
        "the same budget)")


def next_slot(con: sqlite3.Connection, task_class: str) -> int:
    """The slot this class's next test will consume.

    Reads ``MAX(slot) + 1`` without creating anything: a DB that predates
    migration 0089 has no table and its first test is slot 1. Shared by
    :func:`certify` (inside its IMMEDIATE transaction) and the pre-flight
    (outside one), so the two can never disagree about which slot they mean.
    """
    tc = normalize_task_class(task_class)
    try:
        row = con.execute(
            "SELECT COALESCE(MAX(slot), 0) + 1 FROM promotion_certificates "
            "WHERE task_class = ?", (tc,)).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return 1
        raise
    return int(row[0])


def preflight(*, task_class: str, cfg: Mapping, gate_probes: int, slot: int,
              db_error: str | None = None,
              allow_loose_alpha: bool = False) -> dict:
    """Decide whether a certificate run is worth starting at all.

    Called BEFORE any probe dollar is spent (apply_run, next to the
    frozen-kernel check). Every error here is one the run could not have
    recovered from later, and none of them should create an
    ``apply_attempts`` row: a config mistake must not become edit memory.

    ``gate_probes`` is the number of NON-ANCHOR probes the gate actually
    scores — the maximum number of discordant pairs a perfectly discriminating
    candidate could produce, and so ``max_e``'s n. Anchors are excluded
    because the gate's before/after vectors exclude them.

    Returns ``{"ok", "errors", "warnings", "slot", "required_gains",
    "gate_probes"}``.
    """
    errors: list[str] = []
    warnings: list[str] = []

    for err in cfg.get("errors") or ():
        errors.append(f"certificate misconfigured: {err}")
    if db_error:
        errors.append(f"certificate ledger unreachable: {db_error}")

    alpha = float(cfg["alpha"])
    bet = float(cfg["bet"])
    ledger = str(cfg["ledger"])

    if alpha > ALPHA_GUARDRAIL and not allow_loose_alpha:
        errors.append(
            f"certificate alpha={alpha:g} is looser than the {ALPHA_GUARDRAIL:g} "
            f"guardrail; set {ALPHA_LOOSE_ENV}=1 to accept the added false-"
            "promotion risk")
    warn = ledger_warning(ledger)
    if warn:
        warnings.append(warn)

    try:
        required = required_gains(int(slot), alpha, bet, ledger)
    except (TypeError, ValueError) as exc:
        errors.append(f"certificate configuration is unusable: {exc}")
        required = None

    if required is not None and int(gate_probes) < required:
        # Note what NOT to suggest: MO_APPLY_PROBE_MAX_TASKS is a cap, not a
        # supply. Raising it past the number of non-anchor probes the recipe
        # ships changes nothing, so the fix is more probes or a lower bar.
        errors.append(
            f"certificate pre-flight: slot {slot} needs {required} clean "
            f"gains to certify, but the frozen probe set yields only "
            f"{gate_probes} non-anchor gate probe(s) (MO_APPLY_PROBE_MAX_TASKS "
            "caps that number, it cannot add probes); ship more non-anchor "
            "probes, or lower MO_APPLY_CERT_ALPHA / set "
            "MO_APPLY_CERT_LEDGER=none. Refusing before any probe spend.")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "task_class": task_class,
        "slot": int(slot),
        "required_gains": required,
        "gate_probes": int(gate_probes),
    }


def _begin_immediate(con: sqlite3.Connection, attempts: int = 4,
                     base_sleep: float = 0.05) -> None:
    """``BEGIN IMMEDIATE`` with a bounded retry on SQLite's lock contention.

    A busy database is an infrastructure event, not evidence about the
    candidate: the caller distinguishes the two so a one-second lock never
    becomes a permanent "this directive already failed" memory.
    """
    for i in range(attempts):
        try:
            con.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            name = getattr(exc, "sqlite_errorname", "") or ""
            if "SQLITE_BUSY" not in name and "SQLITE_LOCKED" not in name \
                    and "locked" not in str(exc) and "busy" not in str(exc):
                raise
            if i == attempts - 1:
                raise
            time.sleep(base_sleep * (2 ** i))


def _parse_unit_interval(environ: Mapping[str, str], key: str, default: float,
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
    env = os.environ if environ is None else environ
    errors: list[str] = []
    alpha = _parse_unit_interval(env, ALPHA_ENV, DEFAULT_ALPHA, errors)
    bet = _parse_unit_interval(env, BET_ENV, DEFAULT_BET, errors)

    ledger = "spend"
    raw_ledger = env.get(LEDGER_ENV)
    if raw_ledger:
        if raw_ledger in LEDGER_MODES:
            ledger = raw_ledger
        else:
            errors.append(
                f"{LEDGER_ENV}={raw_ledger!r} must be one of "
                f"{', '.join(LEDGER_MODES)}")

    return {
        "enabled": env.get(FLAG) == "1",
        "alpha": alpha,
        "bet": bet,
        "ledger": ledger,
        "errors": errors,
    }


def certify(con: sqlite3.Connection, *, task_class: str, candidate_id: str,
            before: list, after: list, alpha_total: float, bet: float,
            ledger: str, now: int, run_id: str | None = None,
            scorer: str | None = None, dry_run: bool = False,
            probe_set_hash: str | None = None,
            probe_ids: list[str] | None = None) -> dict:
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
        "task_class_raw": task_class,
        "candidate_id": candidate_id,
        "gains": gains,
        "losses": losses,
        "n": len(before),
        "bet": bet,
        "ledger": ledger,
        "alpha_total": alpha_total,
        "e_value": e_value,
        "run_id": run_id,
        "scorer": scorer,
        "dry_run": bool(dry_run),
        "probe_set_hash": probe_set_hash,
        "probe_ids": probe_ids,
        "ledger_warning": ledger_warning(ledger),
    }

    if gains + losses == 0:
        return {
            **base,
            "consumed": False,
            "status": "no_vectors",
            "slot": None,
            "alpha_k": None,
            "threshold": None,
            "required_gains": None,
            "certified": False,
        }

    ensure_table(con)
    try:
        _begin_immediate(con)
        slot = next_slot(con, tc)
        alpha_k = slot_alpha(alpha_total, slot, ledger)
        threshold = 1.0 / alpha_k
        need = required_gains(slot, alpha_total, bet, ledger)
        certified = e_value >= threshold
        if certified:
            status = "certified"
        elif max_e(gains + losses, bet) < threshold:
            # No split of this many discordant pairs could clear the bar.
            # A faithful "unreachable" beats an "uncertified" that invites
            # another identical run.
            status = "unreachable"
        else:
            status = "uncertified"
        con.execute(
            "INSERT INTO promotion_certificates (task_class, task_class_raw, "
            "slot, candidate_id, run_id, scorer, dry_run, probe_set_hash, "
            "probe_ids, gains, losses, n, bet, e_value, ledger, alpha_total, "
            "alpha_k, threshold, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tc, task_class, slot, candidate_id, run_id, scorer,
             1 if dry_run else 0, probe_set_hash,
             json.dumps(probe_ids) if probe_ids is not None else None,
             gains, losses, len(before), bet, e_value, ledger, alpha_total,
             alpha_k, threshold, status, now))
        con.commit()
    except Exception:
        con.rollback()
        raise

    return {
        **base,
        "consumed": True,
        "status": status,
        "slot": slot,
        "alpha_k": alpha_k,
        "threshold": threshold,
        "required_gains": need,
        "certified": certified,
    }
