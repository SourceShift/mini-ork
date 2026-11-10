"""Per-lesson node outcomes: does a lesson help the node that received it?

``lesson_injections`` (0062) records which lesson every node was given and in
which experimental arm. ``node_attempts`` (0050) records how every attempt
ended. The two join on ``(run_id, node_id, attempt = attempt_no)`` and — until
this module — nothing joined them. Every pre-existing tally resolves at *run*
granularity: ``context_item_utility`` credits every item delivered to any node
with one run-level publish, and ``semantic_memory_uses`` stamps the run's
result onto every retrieval. Both lose attribution exactly where it matters.

``resolve_finished`` folds the join into ``lesson_outcome_exposures``, one row
per ``(run, node, lesson, arm)``. ``summarize`` reads it back. Three contrasts
sit on top, and each is labelled for what it is:

* **clean arm** — delivered minus held-out, exact Fisher test. The only
  contrast that gates a decision: a lesson is withheld by a hash of its *own
  id*, so which lessons are in the arm is random with respect to content. It
  reports ``insufficient_held_out`` while the withheld slice is thin rather
  than inventing a number, and it starts reporting by itself the day the arm
  fills.
* **population gap** — the same ``(node_type, task_class)`` stratum over a
  window, direct-standardized to the treated weights. It is correlational:
  a lesson is selected *because* it matched, so this number carries selection
  bias and is never called a lift. It exists to describe, not to rank.

One contrast a reader will expect and that cannot exist: treated node against
untreated node *within* one run. ``_learned_block`` is built once per run and
delivered to every node of the covered types, so a run's primary nodes receive
an identical lesson set — the treated/untreated split is between runs, never
inside one. Measured on the live ledger: 778 of 794 primary node-runs got the
most popular pattern, and no ``(run, node_type)`` cell ever held both a treated
and an untreated but injection-bearing node. There is nothing to pair, so this
module does not pretend to.

Outcome definition. Raw attempt success is 97.8% saturated and first-attempt
success is *worse* (98.9%), so neither can carry a measurement. The primary is
**clean first attempt** — attempt 1 succeeded and there was no attempt 2 —
which folds failure and retry into one variable and still leaves an
implementer/reviewer base rate in the percent range. Attempt 1 is the
observation unit: a lesson first injected at attempt 2 gets no row, because the
node's first try cannot be credited to guidance it never saw.

Scope limit, recorded because it is the honest answer: verifier nodes hold 98
of 125 total failures and 20.8% of retries, and they receive **zero**
injections (``_learned_block`` covers researcher/implementer/reviewer, and the
exclusion is correct — a verifier that reads the lessons it grades is
contaminated). The node type where nodes fail is therefore unmeasurable here.
Researcher is reported as ``saturated`` for the same reason in the other
direction: half the exposures, 0.39% retried.

Anti-fabrication rules, carried over from ``metric_anchor``: an unmeasured rate
is ``None``, never ``0.0``; an unresolved effect contributes exactly ``0.0`` to
a score, never a guessed prior; any database failure yields a neutral value,
never an exception on the injection path.
"""
from __future__ import annotations

import math
import os
import sqlite3
import time
from typing import Iterable, Mapping, Sequence

from mini_ork.context import context_env

__all__ = [
    "DEFAULT_MIN_EVENTS",
    "DEFAULT_MIN_N",
    "DEFAULT_WINDOW_DAYS",
    "PRIMARY_NODE_TYPES",
    "SATURATED_NODE_TYPES",
    "TERMINAL_STATUSES",
    "W_EFFECT",
    "arm_diagnostic",
    "clean_arm_contrast",
    "effect",
    "effects",
    "enabled",
    "exposures",
    "fisher_p",
    "ledger_stats",
    "lesson_summary",
    "population_gap",
    "resolve_finished",
    "resolve_runs",
    "run_level",
    "summaries",
    "summarize",
]

# Terminal statuses: a run in one of these has all the attempts it will ever
# have, so its exposures are final. ``superseded`` and ``not_needed`` are
# terminal too — the attempts inside them still describe a real node run.
TERMINAL_STATUSES = (
    "published",
    "failed",
    "rolled_back",
    "superseded",
    "not_needed",
)

# The node types where a clean-first-attempt rate carries signal. Researcher is
# excluded from the headline (saturated) and verifier receives no lessons.
PRIMARY_NODE_TYPES = frozenset({"implementer", "reviewer"})
SATURATED_NODE_TYPES = frozenset({"researcher"})

# A lesson that measurably hurts may be reordered, never removed.
W_EFFECT = 0.20

DEFAULT_MIN_N = 30
DEFAULT_MIN_EVENTS = 20
DEFAULT_WINDOW_DAYS = 14

# Duplicated from db/migrations/0092_lesson_outcome_exposures.sql on purpose:
# the same bridge ``item_utility`` and ``ledger`` use so a test or dev DB works
# without the migration loader. tests/unit/test_lesson_outcomes_py.py compares
# this DDL against the migration file's, so the two cannot drift apart.
_DDL = (
    "CREATE TABLE IF NOT EXISTS lesson_outcome_exposures ("
    " run_id      TEXT    NOT NULL,"
    " node_id     TEXT    NOT NULL,"
    " source_kind TEXT    NOT NULL,"
    " source_id   TEXT    NOT NULL,"
    " arm         TEXT    NOT NULL CHECK (arm IN ('delivered','held_out')),"
    " stable      INTEGER NOT NULL DEFAULT 1 CHECK (stable IN (0,1)),"
    " node_type   TEXT    NOT NULL DEFAULT '',"
    " task_class  TEXT    NOT NULL DEFAULT '',"
    " outcome_day INTEGER NOT NULL DEFAULT 0,"
    " result_1    TEXT,"
    " clean       INTEGER NOT NULL DEFAULT 0 CHECK (clean IN (0,1)),"
    " retried     INTEGER NOT NULL DEFAULT 0 CHECK (retried IN (0,1)),"
    " failed      INTEGER NOT NULL DEFAULT 0 CHECK (failed IN (0,1)),"
    " attempts    INTEGER NOT NULL DEFAULT 0,"
    " cost_usd    REAL,"
    " resolved_at INTEGER NOT NULL,"
    " PRIMARY KEY (run_id, node_id, source_kind, source_id, arm))",
    "CREATE INDEX IF NOT EXISTS idx_lesson_outcome_exposures_lesson"
    " ON lesson_outcome_exposures(source_kind, source_id, arm)",
    "CREATE INDEX IF NOT EXISTS idx_lesson_outcome_exposures_day"
    " ON lesson_outcome_exposures(outcome_day DESC)",
    "CREATE INDEX IF NOT EXISTS idx_lesson_outcome_exposures_stratum"
    " ON lesson_outcome_exposures(node_type, task_class, outcome_day)",
    "CREATE TABLE IF NOT EXISTS lesson_outcome_runs ("
    " run_id      TEXT PRIMARY KEY,"
    " resolved_at INTEGER NOT NULL,"
    " note        TEXT NOT NULL DEFAULT '')",
    "CREATE INDEX IF NOT EXISTS idx_lesson_outcome_runs_resolved"
    " ON lesson_outcome_runs(resolved_at DESC)",
)

_COLUMNS = (
    "run_id", "node_id", "source_kind", "source_id", "arm", "stable",
    "node_type", "task_class", "outcome_day", "result_1", "clean", "retried",
    "failed", "attempts", "cost_usd", "resolved_at",
)


# ── settings ───────────────────────────────────────────────────────────────

def enabled() -> bool:
    """Whether the resolver runs. Default on; only the exact ``"0"`` opts out."""
    return context_env("MO_LESSON_OUTCOMES", "1") != "0"


def effect_rerank_enabled() -> bool:
    """Whether a measured effect may reorder selection. Default on, and inert
    until some lesson is ``measured``."""
    return context_env("MO_LESSON_EFFECT_RERANK", "1") != "0"


def _int_env(key: str, default: int, *, floor: int = 0) -> int:
    raw = context_env(key, str(default))
    try:
        return max(floor, int(raw))
    except (TypeError, ValueError):
        return default


def min_n() -> int:
    return _int_env("MO_LESSON_OUTCOME_MIN_N", DEFAULT_MIN_N, floor=1)


def min_events() -> int:
    return _int_env("MO_LESSON_OUTCOME_MIN_EVENTS", DEFAULT_MIN_EVENTS, floor=1)


def window_days() -> int:
    return _int_env("MO_LESSON_OUTCOME_WINDOW_DAYS", DEFAULT_WINDOW_DAYS, floor=1)


# ── storage ────────────────────────────────────────────────────────────────

def resolve_db(db: str | None = None) -> str | None:
    """Explicit arg, then ``MINI_ORK_DB``, then ``$MINI_ORK_HOME/state.db``.

    Same resolution order as ``learning.ledger._resolve_db`` so the writer and
    the readers can never disagree about which DB they mean.
    """
    if db:
        return db
    env_db = os.environ.get("MINI_ORK_DB")
    if env_db:
        return env_db
    home = os.environ.get("MINI_ORK_HOME") or ".mini-ork"
    return os.path.join(home, "state.db")


def connect(db: str | None = None) -> sqlite3.Connection | None:
    """Open the resolved DB, or ``None`` when it does not exist.

    Strict on missing: ``sqlite3.connect`` would conjure an empty file and turn
    "the migration has not run" into "the store is empty". Every caller treats
    ``None`` as unavailable and says so, rather than reporting a zero.
    """
    path = resolve_db(db)
    if not path or not os.path.exists(path):
        return None
    try:
        con = sqlite3.connect(path, timeout=5.0)
    except (sqlite3.Error, OSError):
        return None
    try:
        con.execute("PRAGMA busy_timeout=5000")
        con.row_factory = sqlite3.Row
    except sqlite3.Error:
        con.close()
        return None
    return con


def ensure_schema(con: sqlite3.Connection) -> None:
    """Create the two tables when the migration has not run (tests, dev DBs).

    Production DBs get them from ``db/migrations/0092_…sql``; this is the same
    bridge ``item_utility._ensure_tables`` provides, kept byte-comparable by
    the column-set test.
    """
    for statement in _DDL:
        con.execute(statement)


def has_tables(con: sqlite3.Connection) -> bool:
    """Whether both tables exist. A reader that finds neither reports
    ``unavailable`` instead of an empty result."""
    try:
        names = {
            str(row[0])
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name IN ('lesson_outcome_exposures', 'lesson_outcome_runs')"
            ).fetchall()
        }
    except sqlite3.Error:
        return False
    return len(names) == 2


# ── resolution ─────────────────────────────────────────────────────────────

def _node_facts(con: sqlite3.Connection, run_id: str) -> dict[str, dict]:
    """Per-node attempt summary for one run, keyed by ``node_id``.

    ``clean`` needs both halves of the first try: attempt 1 succeeded *and* no
    later attempt exists. A node that succeeded on attempt 3 is not clean — it
    needed two more tries, and that cost is the thing a lesson is supposed to
    remove.
    """
    facts: dict[str, dict] = {}
    rows = con.execute(
        "SELECT node_id, attempt_no, result, cost_usd, started_at, node_type"
        " FROM node_attempts WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    for node_id, attempt_no, result, cost, started, node_type in rows:
        node_id = str(node_id)
        fact = facts.setdefault(node_id, {
            "attempts": 0,
            "max_attempt": 0,
            "result_1": None,
            "failed": 0,
            "cost": None,
            "day": 0,
            "node_type": str(node_type or ""),
        })
        try:
            number = int(attempt_no or 0)
        except (TypeError, ValueError):
            number = 0
        fact["attempts"] += 1
        fact["max_attempt"] = max(fact["max_attempt"], number)
        if number == 1:
            fact["result_1"] = result
        if str(result) == "failure":
            fact["failed"] = 1
        if cost is not None:
            fact["cost"] = (fact["cost"] or 0.0) + float(cost)
        if started and not fact["day"]:
            try:
                fact["day"] = int(started) // 86400
            except (TypeError, ValueError):
                fact["day"] = 0
        if not fact["node_type"] and node_type:
            fact["node_type"] = str(node_type)
    for fact in facts.values():
        fact["clean"] = 1 if (fact["result_1"] == "success"
                              and fact["max_attempt"] <= 1) else 0
        fact["retried"] = 1 if fact["max_attempt"] > 1 else 0
    return facts


def _is_stable(kind: str, source_id: str) -> bool:
    """Whether the id names a lesson rather than a kickoff position.

    ``context_v2`` constraint ids are ``c:<index>`` — the *position* of a
    kickoff constraint, not a thing the loop learned. Two runs' ``c:0`` are
    different constraints, so they are stored with ``stable=0`` and excluded
    from every rate.
    """
    if kind == "context_v2" and source_id.startswith("c:"):
        return False
    return True


def resolve_finished(db: str | None = None, *, limit: int = 200) -> dict:
    """Fold finished runs' exposures into the fact table. Idempotent.

    Runs are taken newest first so the ``limit`` window reaches the recent runs
    that carry injections before spending itself on older history. A run is
    **marked before it is read**, so a run with nothing to grade cannot occupy
    the window forever — the trap ``item_utility.resolve_finished`` documents.

    Returns ``{"runs", "exposures", "skipped", "note"}``; ``note`` is non-empty
    when the store was unavailable, and every count is then 0.
    """
    result = {"runs": 0, "exposures": 0, "skipped": 0, "note": ""}
    if not enabled():
        result["note"] = "disabled"
        return result
    con = connect(db)
    if con is None:
        result["note"] = "unavailable"
        return result
    try:
        ensure_schema(con)
        placeholders = ",".join("?" * len(TERMINAL_STATUSES))
        rows = con.execute(
            "SELECT t.id, COALESCE(t.task_class, '') FROM task_runs t"
            f" WHERE t.status IN ({placeholders})"
            " AND t.id NOT IN (SELECT run_id FROM lesson_outcome_runs)"
            " ORDER BY t.created_at DESC, t.id DESC LIMIT ?",
            (*TERMINAL_STATUSES, int(limit)),
        ).fetchall()
        now = int(time.time())
        for run_id, task_class in rows:
            run_id = str(run_id)
            facts = _node_facts(con, run_id)
            injected = con.execute(
                "SELECT node_id, source_kind, source_id, held_out"
                " FROM lesson_injections WHERE run_id = ? AND attempt = 1",
                (run_id,),
            ).fetchall()
            note = ""
            if not facts:
                note = "no_attempts"
            # Mark first. A run with no attempts, or whose every lesson was
            # withheld, still leaves the window.
            con.execute(
                "INSERT OR IGNORE INTO lesson_outcome_runs"
                " (run_id, resolved_at, note) VALUES (?, ?, ?)",
                (run_id, now, note),
            )
            result["runs"] += 1
            if note:
                continue
            for node_id, kind, source_id, held in injected:
                fact = facts.get(str(node_id))
                if fact is None or fact["result_1"] is None:
                    result["skipped"] += 1
                    continue
                con.execute(
                    "INSERT INTO lesson_outcome_exposures"
                    " (run_id, node_id, source_kind, source_id, arm, stable,"
                    "  node_type, task_class, outcome_day, result_1, clean,"
                    "  retried, failed, attempts, cost_usd, resolved_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(run_id, node_id, source_kind, source_id, arm)"
                    " DO UPDATE SET"
                    "  clean = excluded.clean,"
                    "  retried = excluded.retried,"
                    "  failed = excluded.failed,"
                    "  attempts = excluded.attempts,"
                    "  cost_usd = excluded.cost_usd,"
                    "  resolved_at = excluded.resolved_at",
                    (
                        run_id, str(node_id), str(kind), str(source_id),
                        "held_out" if held else "delivered",
                        1 if _is_stable(str(kind), str(source_id)) else 0,
                        fact["node_type"], str(task_class or ""), fact["day"],
                        fact["result_1"], fact["clean"], fact["retried"],
                        fact["failed"], fact["attempts"], fact["cost"],
                        now,
                    ),
                )
                result["exposures"] += 1
        con.commit()
        return result
    except Exception:  # noqa: BLE001 — a broken store is neutral, never fatal
        try:
            con.rollback()
        except sqlite3.Error:
            pass
        result["note"] = "failed"
        return result
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass


# ── reading ────────────────────────────────────────────────────────────────

def exposures(
    db: str | None = None,
    *,
    kind: str | None = None,
    ident: str | None = None,
    since_day: int | None = None,
) -> list[dict]:
    """Fact rows, oldest-capped by ``since_day`` when given. ``[]`` when the
    store or the tables are missing — the caller decides how to say so."""
    con = connect(db)
    if con is None:
        return []
    try:
        if not has_tables(con):
            return []
        clause = []
        params: list = []
        if kind:
            clause.append("source_kind = ?")
            params.append(kind)
        if ident:
            clause.append("source_id = ?")
            params.append(ident)
        if since_day is not None:
            clause.append("outcome_day >= ?")
            params.append(int(since_day))
        where = f" WHERE {' AND '.join(clause)}" if clause else ""
        rows = con.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM lesson_outcome_exposures{where}",
            params,
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def ledger_stats(db: str | None = None) -> dict:
    """Header numbers for the report: rows, runs, lessons, withheld slice."""
    out = {
        "available": False, "rows": 0, "runs": 0, "lessons": 0,
        "held_out_rows": 0, "held_out_lessons": 0, "resolved_runs": 0,
        "resolved_at": None,
    }
    con = connect(db)
    if con is None:
        return out
    try:
        if not has_tables(con):
            return out
        out["available"] = True
        row = con.execute(
            "SELECT COUNT(*), COUNT(DISTINCT run_id),"
            " COUNT(DISTINCT source_kind || ':' || source_id)"
            " FROM lesson_outcome_exposures WHERE stable = 1"
        ).fetchone()
        out["rows"], out["runs"], out["lessons"] = (
            int(row[0] or 0), int(row[1] or 0), int(row[2] or 0))
        held = con.execute(
            "SELECT COUNT(*), COUNT(DISTINCT source_kind || ':' || source_id)"
            " FROM lesson_outcome_exposures WHERE stable = 1 AND arm = 'held_out'"
        ).fetchone()
        out["held_out_rows"] = int(held[0] or 0)
        out["held_out_lessons"] = int(held[1] or 0)
        marker = con.execute(
            "SELECT COUNT(*), MAX(resolved_at) FROM lesson_outcome_runs"
        ).fetchone()
        out["resolved_runs"] = int(marker[0] or 0)
        out["resolved_at"] = int(marker[1]) if marker[1] else None
        return out
    except sqlite3.Error:
        return out
    finally:
        con.close()


def arm_diagnostic(db: str | None = None, *, sample: int = 10) -> dict:
    """Is the withheld slice reproducible from today's arm predicate?

    Reads ``lesson_injections`` and asks the renderer's own predicate
    (``context_assembler.lesson_held_out``) about every lesson it has ever
    recorded. Two kinds of disagreement are possible and both are reported
    rather than resolved here:

    * ``inconsistent`` — a lesson with held rows that today's bucket would not
      withhold. Either the rate was different when the row was written (it is
      an operator dial) or a second writer produced the flag. Either way the
      row is in the ledger and the clean arm must not pretend otherwise.
    * ``missing_expected`` — a lesson inside the arm that has produced a held
      row *nowhere* while still being injected at volume **since the arm
      existed**. Only those can be a missing sink. A lesson whose injections
      all predate the first held row is history: the holdout shipped on
      2026-11-08 and ``theme:th-1e42cc51c68c`` (bucket 0.018, squarely in the
      arm) is 641 rows that stop on 2026-10-15 — nothing is broken, it simply
      stopped being selected before there was an arm to withhold it from.
      Scoring it as a defect would send the fix to a channel that works.

    The evidence for the first case, measured 2026-11-10: two runs hold
    ``pattern:pat-a7551171398c`` (bucket 0.6053) on a reviewer node while
    delivering it to four other nodes of the same run — a per-node split no
    id-level predicate can produce, and 97 of that run's 99 rows unwithheld, so
    no single rate explains it either. Cause unresolved; recorded, not guessed.
    """
    out = {
        "status": "unavailable", "rate": None, "lessons": 0,
        "held_rows": 0, "held_lessons": 0, "arm_start": None,
        "inconsistent": [], "inconsistent_count": 0,
        "missing_expected": [], "missing_expected_count": 0,
    }
    con = connect(db)
    if con is None:
        return out
    try:
        if not has_tables(con):
            # The ledger predates 0092, so the probe still works without it.
            names = {str(r[0]) for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "lesson_injections" not in names:
                return out
        arm_start = con.execute(
            "SELECT MIN(ts) FROM lesson_injections WHERE held_out = 1"
        ).fetchone()[0]
        # Two counts per lesson: everything, and everything since the first
        # held row. The second is what "a sink is missing" has to be judged on.
        rows = con.execute(
            "SELECT source_kind, source_id, COUNT(*), SUM(held_out),"
            " SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END)"
            " FROM lesson_injections GROUP BY source_kind, source_id",
            (arm_start if arm_start is not None else 0,),
        ).fetchall()
    except sqlite3.Error:
        return out
    finally:
        con.close()
    try:
        from mini_ork import context_assembler  # noqa: PLC0415 — heavy module

        rate = context_assembler._lesson_holdout_rate()
        in_arm = context_assembler.lesson_held_out
    except Exception:  # noqa: BLE001 — diagnostics never break the report
        return out
    out["status"] = "measured"
    out["rate"] = rate
    out["arm_start"] = int(arm_start) if arm_start is not None else None
    out["lessons"] = len(rows)
    inconsistent: list[dict] = []
    missing: list[dict] = []
    for kind, ident, total, held, since_arm in rows:
        held = int(held or 0)
        total = int(total or 0)
        since_arm = int(since_arm or 0)
        key = f"{kind}:{ident}"
        expected = in_arm(str(kind), str(ident))
        out["held_rows"] += held
        if held:
            out["held_lessons"] += 1
        if held and not expected:
            inconsistent.append({"lesson": key, "held": held, "n": total})
        elif (expected and not held and since_arm >= DEFAULT_MIN_N):
            missing.append({"lesson": key, "n": total, "since_arm": since_arm})
    out["inconsistent_count"] = len(inconsistent)
    out["inconsistent"] = sorted(
        inconsistent, key=lambda e: -e["n"])[:sample]
    out["missing_expected_count"] = len(missing)
    out["missing_expected"] = sorted(
        missing, key=lambda e: -e["n"])[:sample]
    return out


def resolve_runs(db: str | None = None, *, limit: int = 200) -> list[dict]:
    """The marker rows, newest first — what ``--runs`` prints.

    Auditing aid: ``note`` carries ``no_attempts`` for a terminal run whose
    attempts were never written, so a permanently ungradeable run is visible
    instead of silently absent from the denominators.
    """
    con = connect(db)
    if con is None:
        return []
    try:
        if not has_tables(con):
            return []
        rows = con.execute(
            "SELECT run_id, resolved_at, note FROM lesson_outcome_runs"
            " ORDER BY resolved_at DESC, run_id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _rate(num: int, den: int) -> float | None:
    """A rate, or ``None`` when the denominator is zero — never ``0.0``."""
    return round(num / den, 4) if den else None


def _lesson_key(row: Mapping) -> str:
    return f"{row.get('source_kind')}:{row.get('source_id')}"


def _arm_status(rows: Sequence[Mapping], *, saturated_only: bool,
                min_n_value: int, min_events_value: int) -> str:
    n = len(rows)
    if n == 0:
        return "insufficient_n"
    if saturated_only:
        return "saturated"
    if n < min_n_value:
        return "insufficient_n"
    non_clean = sum(1 for row in rows if not int(row.get("clean") or 0))
    if non_clean < min_events_value:
        return "insufficient_events"
    return "measured"


def summarize(
    rows: Iterable[Mapping],
    *,
    min_n_value: int | None = None,
    min_events_value: int | None = None,
) -> dict:
    """Pure rollup over fact rows. No DB, so it is unit-testable on its own.

    Groups by ``(source_kind, source_id, arm)``. ``stable=0`` rows are counted
    in ``unstable`` and excluded from every rate. Status is computed over the
    *stable* rows only, and the saturated verdict follows the node types the
    rows actually carry.
    """
    lo_n = min_n() if min_n_value is None else min_n_value
    lo_events = min_events() if min_events_value is None else min_events_value
    by_lesson: dict[str, dict[str, list[Mapping]]] = {}
    unstable = 0
    for row in rows:
        if not int(row.get("stable") or 0):
            unstable += 1
            continue
        arm = str(row.get("arm") or "")
        by_lesson.setdefault(_lesson_key(row), {}).setdefault(arm, []).append(row)

    lessons: list[dict] = []
    for key, arms in sorted(by_lesson.items()):
        entry: dict = {"lesson": key, "arms": {}, "unstable": unstable}
        kinds = set()
        for arm, arm_rows in sorted(arms.items()):
            n = len(arm_rows)
            clean = sum(1 for r in arm_rows if int(r.get("clean") or 0))
            retried = sum(1 for r in arm_rows if int(r.get("retried") or 0))
            failed = sum(1 for r in arm_rows if int(r.get("failed") or 0))
            node_types = sorted({str(r.get("node_type") or "") for r in arm_rows} - {""})
            kinds.update(node_types)
            # The primary split is what decides whether a lesson is usable:
            # researcher rows are half the ledger and nearly all clean, so a
            # mixed pool reads "measured" on volume it cannot learn from.
            primary_rows = [r for r in arm_rows
                            if str(r.get("node_type") or "") in PRIMARY_NODE_TYPES]
            p_n = len(primary_rows)
            p_clean = sum(1 for r in primary_rows if int(r.get("clean") or 0))
            entry["arms"][arm] = {
                "n_nodes": n,
                "clean": clean,
                "clean_rate": _rate(clean, n),
                "retried": retried,
                "retry_rate": _rate(retried, n),
                "failed": failed,
                "fail_rate": _rate(failed, n),
                "node_types": node_types,
                "n_primary": p_n,
                "clean_primary": p_clean,
                "clean_rate_primary": _rate(p_clean, p_n),
                "primary_status": _arm_status(
                    primary_rows,
                    saturated_only=False,
                    min_n_value=lo_n,
                    min_events_value=lo_events,
                ),
                "status": _arm_status(
                    arm_rows,
                    saturated_only=bool(node_types) and set(node_types) <= SATURATED_NODE_TYPES,
                    min_n_value=lo_n,
                    min_events_value=lo_events,
                ),
            }
        delivered = entry["arms"].get("delivered", {})
        control = entry["arms"].get("held_out", {})
        entry["node_types"] = sorted(kinds)
        entry["primary_only"] = bool(kinds) and kinds <= SATURATED_NODE_TYPES
        entry["clean_lift"] = _clean_lift(
            _primary_view(delivered), _primary_view(control), lo_n, lo_events)
        lessons.append(entry)
    return {
        "lessons": lessons,
        "unstable": unstable,
        "min_n": lo_n,
        "min_events": lo_events,
    }


def _primary_view(arm: Mapping) -> dict:
    """An arm dict as seen on primary node types only.

    Falls back to the all-type numbers when the arm has *no* primary rows, so a
    lesson that only ever reached researcher nodes still reports something —
    labelled by the saturated status its caller computed.
    """
    if not arm:
        return {}
    if int(arm.get("n_primary") or 0):
        return {"n_nodes": int(arm["n_primary"]), "clean": int(arm["clean_primary"])}
    return {"n_nodes": int(arm.get("n_nodes") or 0), "clean": int(arm.get("clean") or 0)}


def _clean_lift(delivered: Mapping, control: Mapping,
                lo_n: int, lo_events: int) -> dict:
    """Delivered minus held-out clean rate, or a named insufficiency.

    ``status`` says which arm is short and never shades into a number: with a
    six-row control this must read ``insufficient_n``, not a plausible-looking
    delta over n=6.
    """
    n_del = int(delivered.get("n_nodes") or 0)
    n_ctl = int(control.get("n_nodes") or 0)
    out = {
        "value": None,
        "status": "insufficient",
        "n_delivered": n_del,
        "n_held_out": n_ctl,
        "need": lo_n,
    }
    if n_del < lo_n:
        out["status"] = "insufficient_delivered"
        return out
    if n_ctl < lo_n:
        out["status"] = "insufficient_held_out"
        return out
    del_events = n_del - int(delivered.get("clean") or 0)
    ctl_events = n_ctl - int(control.get("clean") or 0)
    if del_events < lo_events or ctl_events < lo_events:
        out["status"] = "insufficient_events"
        return out
    del_rate = _rate(int(delivered.get("clean") or 0), n_del)
    ctl_rate = _rate(int(control.get("clean") or 0), n_ctl)
    if del_rate is None or ctl_rate is None:
        return out
    out["value"] = round(float(del_rate) - float(ctl_rate), 4)
    out["status"] = "measured"
    return out


def fisher_p(a_clean: int, a_total: int, b_clean: int, b_total: int) -> float | None:
    """Exact two-sided Fisher test on two clean/total pairs.

    This is the right test for the one contrast the ledger can support: a
    lesson is withheld by a hash of its own id, so *which lessons* are in the
    arm is random — but for a given lesson every node that saw it either saw it
    or did not, at run granularity. There is no within-run pairing to exploit
    (``_learned_block`` is built once per run and delivered to every node of
    the types it covers, so a run's primary nodes all receive the same set), so
    a sign test over pairs would be structurally empty — 0 observations, not
    few. Two independent proportions are the honest shape, and Fisher is exact
    at the counts available (dozens, not thousands).

    ``None`` when either arm is empty — an untested contrast, never p = 1.
    """
    if a_total <= 0 or b_total <= 0:
        return None
    n = a_total + b_total
    k = a_clean + b_clean
    lo = max(0, k - b_total)
    hi = min(a_total, k)
    denom = math.comb(n, k)
    if denom <= 0:
        return None

    def _pmf(x: int) -> int:
        return math.comb(a_total, x) * math.comb(b_total, k - x)

    observed = _pmf(a_clean)
    total = sum(_pmf(x) for x in range(lo, hi + 1) if _pmf(x) <= observed)
    return min(1.0, total / denom)


def clean_arm_contrast(
    db: str | None = None,
    *,
    kind: str,
    ident: str,
    since_day: int | None = None,
    min_n_value: int | None = None,
    min_events_value: int | None = None,
) -> dict:
    """Delivered minus held-out on primary node types, with an exact p.

    The single causal-looking contrast in the module, and the only thing
    allowed to change selection. Its power comes entirely from the arm being
    full: with the withheld slice at a handful of nodes it returns
    ``insufficient_held_out`` and a ``None`` p, and it starts reporting by
    itself the day the arm reaches ``MIN_N`` nodes with ``MIN_EVENTS``
    non-clean events — no code change, no config change.
    """
    rows = [r for r in exposures(db, kind=kind, ident=ident, since_day=since_day)
            if int(r.get("stable") or 0)]
    return _arm_from_rows(rows, min_n_value=min_n_value,
                          min_events_value=min_events_value)


def _arm_from_rows(
    rows: Sequence[Mapping],
    *,
    min_n_value: int | None = None,
    min_events_value: int | None = None,
) -> dict:
    """The delivered/held_out comparison over an in-memory row slice."""
    lo_n = min_n() if min_n_value is None else min_n_value
    lo_events = min_events() if min_events_value is None else min_events_value
    out = {
        "status": "unavailable", "value": None, "p": None,
        "n_delivered": 0, "n_held_out": 0,
        "clean_delivered": 0, "clean_held_out": 0,
        "rate_delivered": None, "rate_held_out": None, "need": lo_n,
    }
    rows = [r for r in rows
            if int(r.get("stable") or 0)
            and str(r.get("node_type") or "") in PRIMARY_NODE_TYPES]
    if not rows:
        return out
    out["status"] = "insufficient_held_out"
    delivered = [r for r in rows if str(r.get("arm")) == "delivered"]
    held = [r for r in rows if str(r.get("arm")) == "held_out"]
    d_n, d_clean = len(delivered), sum(1 for r in delivered if int(r.get("clean") or 0))
    h_n, h_clean = len(held), sum(1 for r in held if int(r.get("clean") or 0))
    out.update({
        "n_delivered": d_n, "n_held_out": h_n,
        "clean_delivered": d_clean, "clean_held_out": h_clean,
        "rate_delivered": _rate(d_clean, d_n),
        "rate_held_out": _rate(h_clean, h_n),
    })
    if d_n < lo_n:
        out["status"] = "insufficient_delivered"
        return out
    if h_n < lo_n:
        return out
    if (d_n - d_clean) < lo_events or (h_n - h_clean) < lo_events:
        out["status"] = "insufficient_events"
        return out
    p = fisher_p(d_clean, d_n, h_clean, h_n)
    out["p"] = p
    if p is None or p >= 0.05:
        out["status"] = "not_significant"
        return out
    out["value"] = round((d_clean / d_n) - (h_clean / h_n), 4)
    out["status"] = "measured"
    return out

def population_gap(
    db: str | None = None,
    *,
    kind: str,
    ident: str,
    window: int | None = None,
    min_n_value: int | None = None,
    since_day: int | None = None,
) -> dict:
    """Descriptive only: this lesson vs its stratum peers that did not get it.

    Strata are ``(node_type, task_class)`` inside ``window`` days of the latest
    exposure, and the two rates are **direct-standardized to the treated
    weights** — otherwise a lesson that only ever reaches researcher nodes,
    which barely fail, would look better than one serving implementers.

    It is labelled ``measured_correlational`` when it produces a number, because
    a lesson is selected *because* it matched the task: this is association.
    Nothing gates on it.
    """
    rows = exposures(db)
    return _gap_from_rows(rows, kind=kind, ident=ident, window=window,
                          min_n_value=min_n_value, since_day=since_day)


def _gap_from_rows(
    rows: Sequence[Mapping],
    *,
    kind: str,
    ident: str,
    window: int | None = None,
    min_n_value: int | None = None,
    since_day: int | None = None,
) -> dict:
    """The descriptive gap over an in-memory row slice (see ``population_gap``)."""
    lo_n = min_n() if min_n_value is None else min_n_value
    days = window_days() if window is None else int(window)
    out = {
        "status": "unavailable", "value": None, "treated_rate": None,
        "control_rate": None, "treated_n": 0, "control_n": 0,
        "treated_events": 0, "control_events": 0, "coverage": None,
        "strata_used": 0, "strata_dropped": 0, "window_days": days,
    }
    rows = [r for r in rows if int(r.get("stable") or 0)]
    if not rows:
        return out
    latest = max(int(r.get("outcome_day") or 0) for r in rows)
    cutoff = latest - days
    if since_day is not None:
        cutoff = max(cutoff, int(since_day))
    here = [r for r in rows if int(r.get("outcome_day") or 0) >= cutoff]
    treated: dict[tuple[str, str], list[Mapping]] = {}
    pool: dict[tuple[str, str], list[Mapping]] = {}
    for row in here:
        stratum = (str(row.get("node_type") or ""), str(row.get("task_class") or ""))
        is_target = (str(row.get("source_kind")) == kind
                     and str(row.get("source_id")) == ident
                     and str(row.get("arm")) == "delivered")
        if is_target:
            treated.setdefault(stratum, []).append(row)
        if str(row.get("arm")) == "delivered":
            pool.setdefault(stratum, []).append(row)
    if not treated:
        return out
    treated_total = sum(len(v) for v in treated.values())
    weighted_t = weighted_c = 0.0
    kept_t = kept_c = kept_t_events = kept_c_events = 0
    strata_used = strata_dropped = 0
    for stratum, t_rows in sorted(treated.items()):
        p_rows = [row for row in pool.get(stratum, [])
                  if not (str(row.get("source_kind")) == kind
                          and str(row.get("source_id")) == ident
                          and str(row.get("arm")) == "delivered")]
        if not p_rows:
            strata_dropped += 1
            continue
        strata_used += 1
        t_n = len(t_rows)
        t_clean = sum(int(r.get("clean") or 0) for r in t_rows)
        c_n = len(p_rows)
        c_clean = sum(int(r.get("clean") or 0) for r in p_rows)
        weight = t_n / treated_total
        weighted_t += weight * (t_clean / t_n)
        weighted_c += weight * (c_clean / c_n)
        kept_t += t_n
        kept_c += c_n
        kept_t_events += t_n - t_clean
        kept_c_events += c_n - c_clean
    if not strata_used or not kept_c:
        out["status"] = "insufficient_strata"
        out["strata_dropped"] = strata_dropped
        return out
    out.update({
        "treated_rate": round(weighted_t, 4),
        "control_rate": round(weighted_c, 4),
        "treated_n": kept_t,
        "control_n": kept_c,
        "treated_events": kept_t_events,
        "control_events": kept_c_events,
        "coverage": round(kept_t / treated_total, 4) if treated_total else None,
        "strata_used": strata_used,
        "strata_dropped": strata_dropped,
    })
    if kept_t >= lo_n and kept_c >= lo_n and kept_t_events and kept_c_events:
        out["value"] = round(weighted_t - weighted_c, 4)
        out["status"] = "measured_correlational"
    elif kept_t < lo_n or kept_c < lo_n:
        out["status"] = "insufficient_n"
    else:
        out["status"] = "insufficient_events"
    return out


def run_level(db: str | None = None, *, kind: str, ident: str) -> dict:
    """Run publish rate for runs that delivered this lesson. Context, not
    attribution: one run's result is shared by every lesson it carried."""
    out = {"status": "unavailable", "wins": 0, "losses": 0, "rate": None}
    con = connect(db)
    if con is None:
        return out
    try:
        if not has_tables(con):
            return out
        rows = con.execute(
            "SELECT t.status, COUNT(DISTINCT e.run_id)"
            " FROM lesson_outcome_exposures e"
            " JOIN task_runs t ON t.id = e.run_id"
            " WHERE e.source_kind = ? AND e.source_id = ? AND e.arm = 'delivered'"
            " AND e.stable = 1 GROUP BY t.status",
            (kind, ident),
        ).fetchall()
    except sqlite3.Error:
        return out
    finally:
        con.close()
    wins = losses = 0
    for status, count in rows:
        if str(status) == "published":
            wins += int(count or 0)
        elif str(status) in ("failed", "rolled_back"):
            losses += int(count or 0)
    out.update({"status": "measured", "wins": wins, "losses": losses,
                "rate": _rate(wins, wins + losses)})
    return out


def effect(db: str | None = None, *, kind: str, ident: str) -> dict:
    """The scalar selection may act on, or a named reason it may not.

    Deliberately strict, because this is the only function whose answer changes
    what a future run's prompt contains:

    * the lesson must be **measured** on primary node types alone (n and
      non-clean events both past their floors),
    * the clean arm must be significant under an exact Fisher test — a point
      estimate never prunes, only evidence does,
    * the population control must cover at least half the treated exposures,
      or the delta is not attributable at all,
    * and the magnitude is the arm's own delta, not the population gap: the
      penalty has to come from the same randomised estimate that earned it.

    ``value`` is ``0.0`` when it refuses, which is exactly today's behaviour.
    """
    full = exposures(db)
    own = [r for r in full
           if str(r.get("source_kind")) == kind and str(r.get("source_id")) == ident]
    return _effect_from_rows(own, kind=kind, ident=ident, pool=full)


def _effect_from_rows(rows: Sequence[Mapping], *, kind: str, ident: str,
                      pool: Sequence[Mapping] | None = None) -> dict:
    """The rerank gate over an in-memory row slice (see ``effect``)."""
    out = {
        "value": 0.0, "status": "disabled", "n": 0, "p": None,
        "coverage": None, "source": None,
    }
    if not effect_rerank_enabled():
        return out
    all_rows = [r for r in rows if int(r.get("stable") or 0)]
    rows = [r for r in all_rows
            if str(r.get("arm")) == "delivered"
            and str(r.get("node_type") or "") in PRIMARY_NODE_TYPES]
    n = len(rows)
    out["n"] = n
    out["status"] = "insufficient_n"
    if n < min_n():
        return out
    if n - sum(int(r.get("clean") or 0) for r in rows) < min_events():
        out["status"] = "insufficient_events"
        return out
    arm = _arm_from_rows(all_rows)
    out["p"] = arm.get("p")
    out["arm"] = {"n_delivered": arm.get("n_delivered"),
                  "n_held_out": arm.get("n_held_out")}
    if arm.get("status") != "measured":
        out["status"] = str(arm.get("status") or "insufficient_held_out")
        return out
    # The magnitude is the arm's own delta — the randomised estimate. Taking it
    # from the population gap instead would gate on a causal test and then size
    # the penalty from an association, which is two estimators wearing one name.
    # Both denominators are past ``min_n`` here (``_arm_from_rows`` only reports
    # ``measured`` when they are), so neither division can see zero.
    d_n = int(arm.get("n_delivered") or 0)
    h_n = int(arm.get("n_held_out") or 0)
    d_clean = int(arm.get("clean_delivered") or 0)
    h_clean = int(arm.get("clean_held_out") or 0)
    # The control pool must still be the *other* lessons' exposures; the
    # lesson's own rows are excluded inside ``_gap_from_rows`` by
    # (kind, id, delivered).
    gap = _gap_from_rows(list(all_rows) + list(pool or []), kind=kind, ident=ident)
    out["coverage"] = gap.get("coverage")
    if (gap.get("coverage") or 0.0) < 0.5:
        out["status"] = "low_coverage"
        return out
    value = (d_clean / d_n) - (h_clean / h_n)
    out["value"] = max(-1.0, min(1.0, value))
    out["status"] = "measured"
    out["source"] = "clean_arm"
    return out


def effects(pairs: Iterable[tuple[str, str]], *, db: str | None = None) -> dict:
    """``{kind:id: effect}`` for the candidates one selection is choosing among.

    One read for the whole set: the rerank runs on the run-start path, where a
    connection (let alone three) per candidate would be the wrong trade. The
    population control needs the pool of *other* lessons, which the same read
    already holds.

    A key absent from the result never appeared in the store; the caller treats
    it as ``0.0``, which is exactly the pre-existing score.
    """
    want = {(str(k), str(i)) for k, i in pairs if k and i}
    if not want:
        return {}
    grouped: dict[tuple[str, str], list[Mapping]] = {}
    pool: list[Mapping] = []
    for row in exposures(db):
        if not int(row.get("stable") or 0):
            continue
        # The gap's pool is every delivered exposure in the window, not just
        # the candidates' rows — a control drawn from the candidates alone
        # would be a comparison between the things being ranked.
        pool.append(row)
        key = (str(row.get("source_kind")), str(row.get("source_id")))
        if key in want:
            grouped.setdefault(key, []).append(row)
    # Cheap gate: without a single withheld row on a candidate, no clean arm
    # can be measured, so the answer is 0.0 for every one of them. This is the
    # state of the ledger today, and it keeps the run-start path at one indexed
    # read instead of a per-candidate standardization over the whole pool.
    if not any(str(r.get("arm")) == "held_out"
               for rows in grouped.values() for r in rows):
        return {}
    out: dict[str, dict] = {}
    for key, rows in grouped.items():
        out[f"{key[0]}:{key[1]}"] = _effect_from_rows(
            rows, kind=key[0], ident=key[1], pool=pool)
    return out


def _summary_from_rows(rows: Sequence[Mapping]) -> dict:
    """The per-lesson headline, computed over an in-memory row slice."""
    if not rows:
        return {"status": "unavailable", "n_nodes": 0, "clean_rate": None,
                "n_primary": 0, "clean_rate_primary": None}
    delivered = [r for r in rows if str(r.get("arm")) == "delivered"]
    n = len(delivered)
    clean = sum(1 for r in delivered if int(r.get("clean") or 0))
    primary = [r for r in delivered
               if str(r.get("node_type") or "") in PRIMARY_NODE_TYPES]
    p_n = len(primary)
    p_clean = sum(1 for r in primary if int(r.get("clean") or 0))
    if p_n:
        status = _arm_status(primary, saturated_only=False,
                             min_n_value=min_n(), min_events_value=min_events())
    else:
        status = _arm_status(
            delivered,
            saturated_only=bool(delivered) and
            {str(r.get("node_type") or "") for r in delivered} <= SATURATED_NODE_TYPES,
            min_n_value=min_n(),
            min_events_value=min_events(),
        )
    return {
        "status": status,
        "n_nodes": n,
        "clean": clean,
        "clean_rate": _rate(clean, n),
        "n_primary": p_n,
        "clean_rate_primary": _rate(p_clean, p_n),
        "held_out": sum(1 for r in rows if str(r.get("arm")) == "held_out"),
    }


def lesson_summary(db: str | None = None, *, kind: str, ident: str) -> dict:
    """One lesson's headline for ``why`` / the learn pages. Cheap; no resolve.

    ``status`` is the primary-node verdict when the lesson reached any primary
    node, because that is the only slice an operator can act on. The all-type
    numbers stay alongside it so a researcher-heavy lesson cannot look better
    than it is.
    """
    return _summary_from_rows([r for r in exposures(db, kind=kind, ident=ident)
                               if int(r.get("stable") or 0)])


def summaries(pairs: Iterable[tuple[str, str]], *, db: str | None = None) -> dict:
    """``{kind:id: summary}`` for many lessons in one read.

    ``why`` prints a dozen ids per node; one query and one grouping beats a
    connection per id. A pair absent from the store is absent from the result —
    the caller says "no outcomes yet", it is never a zero.
    """
    want = {(str(k), str(i)) for k, i in pairs if k and i}
    if not want:
        return {}
    grouped: dict[str, list[Mapping]] = {}
    for row in exposures(db):
        key = (str(row.get("source_kind")), str(row.get("source_id")))
        if key in want and int(row.get("stable") or 0):
            grouped.setdefault(f"{key[0]}:{key[1]}", []).append(row)
    return {key: _summary_from_rows(rows) for key, rows in grouped.items()}
