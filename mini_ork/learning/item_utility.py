"""Per-item utility for the v2 context pack (SimUtil-UCB for themes/findings).

The per-run injection ledger (``runs/<id>/learned/<node>.json``) records which
pack items each node received (``sources[].id``) and whether the node was held
out of the treatment. ``resolve_finished`` folds the outcome of finished runs
into a ``uses``/``wins`` tally, and ``rerank`` reorders a pack section by the
same SimUtil-UCB score the semantic channel already uses — importing the weights
and both closed-form terms from ``mini_ork.memory.semantic`` so the two
channels cannot drift.

Every function is fail-open: a missing table, a corrupt ledger file or an
unreadable database yields a neutral value (0 counted, or the incoming order),
never an exception on the injection path.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

# The formula is shared, not re-derived: importing these bindings also exposes
# them on this module, and the identity check in the tests proves they are the
# semantic module's objects rather than copies.
from mini_ork.memory.semantic import (
    UTILITY_NEUTRAL,
    W_EXPLORE,
    W_UTILITY,
    _exploration,
    _utility,
)

__all__ = [
    "enabled",
    "rerank",
    "resolve_finished",
    "score",
]

# Terminal statuses that carry an outcome. ``published`` is a win; the two
# rollback statuses are losses; everything else (``not_needed``, an in-flight
# status, an unknown value) is skipped until it has a real outcome.
_TERMINAL_STATUSES = ("published", "failed", "rolled_back")

_DDL = (
    "CREATE TABLE IF NOT EXISTS context_item_utility ("
    " item_id   TEXT PRIMARY KEY,"
    " uses      INTEGER NOT NULL DEFAULT 0,"
    " wins      INTEGER NOT NULL DEFAULT 0,"
    " last_used INTEGER)",
    "CREATE TABLE IF NOT EXISTS context_item_runs ("
    " run_id     TEXT PRIMARY KEY,"
    " counted_at INTEGER NOT NULL)",
)


def enabled() -> bool:
    """Whether the v2 pack should rank by item utility. Default on; only the
    exact value ``"0"`` opts out."""
    return os.environ.get("MO_ITEM_UCB") != "0"


def score(similarity: float, uses: int, wins: int, n_total: int) -> float:
    """SimUtil-UCB composite: the same formula ``semantic.rank_with_prior``
    applies to keyed memories, with the external prior standing in for
    similarity."""
    return (
        similarity
        + W_UTILITY * (_utility(uses, wins) - UTILITY_NEUTRAL)
        + W_EXPLORE * _exploration(uses, n_total)
    )


def _connect(db: str | None) -> sqlite3.Connection:
    return sqlite3.connect(db or ":memory:", timeout=5.0)


def _ensure_tables(con: sqlite3.Connection) -> None:
    for statement in _DDL:
        con.execute(statement)


def _read_learned(home: str, run_id: str) -> list[dict]:
    """The parseable ledger records under ``<home>/runs/<run_id>/learned``.

    A corrupt file is skipped on its own; the rest of the directory still
    counts. A missing directory (or a run with no home) is no records, which
    the caller treats as "counted, nothing delivered"."""
    if not home:
        return []
    learned = os.path.join(home, "runs", run_id, "learned")
    try:
        names = sorted(os.listdir(learned))
    except OSError:
        return []
    records: list[dict] = []
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(learned, name), encoding="utf-8") as fh:
                record = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _delivered_ids(records: list[dict]) -> set[str]:
    """Distinct item ids from records that were actually delivered to a node.

    A held-out record's sources were withheld from that node, so they are not
    evidence of delivery; mixing one held-out node with a treated one keeps the
    run (the kickoff only skips a run when *every* record is held out) but
    still ignores the withheld node's ids."""
    ids: set[str] = set()
    for record in records:
        if record.get("held_out") is True:
            continue
        for source in record.get("sources") or []:
            if isinstance(source, dict):
                item_id = source.get("id")
                if item_id:
                    ids.add(str(item_id))
    return ids


def resolve_finished(home: str, db: str, *, limit: int = 200) -> int:
    """Tally finished runs' ledgers into ``context_item_utility``.

    Runs are counted newest first: the ``limit`` window must reach the recent
    runs that carry ``learned/`` ledgers before spending itself on older
    history that has none.

    Counts at most ``limit`` terminal runs not already recorded in
    ``context_item_runs``. A run whose every ledger record is held out
    contributes no evidence, so it is not counted — but it *is* marked, or it
    would occupy the ``limit`` window forever and starve later runs. A run
    with no ``learned/`` directory is marked counted with no ids. Returns the
    number of runs counted, and 0 on any database/IO failure.
    """
    try:
        con = _connect(db)
    except (sqlite3.Error, OSError):
        return 0
    try:
        _ensure_tables(con)
        placeholders = ",".join("?" * len(_TERMINAL_STATUSES))
        rows = con.execute(
            "SELECT id, status FROM task_runs"
            f" WHERE status IN ({placeholders})"
            " AND id NOT IN (SELECT run_id FROM context_item_runs)"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (*_TERMINAL_STATUSES, int(limit)),
        ).fetchall()
        now = int(time.time())
        counted = 0
        for run_id, status in rows:
            run_id = str(run_id)
            records = _read_learned(home, run_id)
            # Mark first: an all-held-out run has nothing to count, but an
            # unmarked one never leaves the ``limit`` window, so 200 holdout
            # runs would wedge the tally permanently.
            con.execute(
                "INSERT OR IGNORE INTO context_item_runs(run_id, counted_at)"
                " VALUES (?, ?)",
                (run_id, now),
            )
            if records and all(r.get("held_out") is True for r in records):
                continue
            win = 1 if str(status) == "published" else 0
            for item_id in sorted(_delivered_ids(records)):
                con.execute(
                    "INSERT INTO context_item_utility"
                    " (item_id, uses, wins, last_used) VALUES (?, 1, ?, ?)"
                    " ON CONFLICT(item_id) DO UPDATE SET"
                    " uses = uses + 1,"
                    " wins = wins + excluded.wins,"
                    " last_used = excluded.last_used",
                    (item_id, win, now),
                )
            counted += 1
        con.commit()
        return counted
    except Exception:  # noqa: BLE001 — injection path: a broken store is neutral
        try:
            con.rollback()
        except sqlite3.Error:
            pass
        return 0
    finally:
        con.close()


def _stats(db: str | None, item_ids: list[str]) -> dict[str, tuple[int, int]]:
    con = _connect(db)
    try:
        _ensure_tables(con)
        if not item_ids:
            return {}
        marks = ",".join("?" * len(item_ids))
        rows = con.execute(
            "SELECT item_id, uses, wins FROM context_item_utility"
            f" WHERE item_id IN ({marks})",
            item_ids,
        ).fetchall()
        return {
            str(row[0]): (int(row[1] or 0), int(row[2] or 0)) for row in rows
        }
    finally:
        con.close()


def rerank(
    items: list[dict], *, db: str, similarity_key: str | None = None
) -> list[dict]:
    """Order ``items`` (dicts carrying an ``"id"``) by :func:`score`.

    With no ``similarity_key`` the incoming order *is* the prior: base
    similarity is ``1.0 - rank / len(items)``, so an untried set reproduces the
    old order exactly and utility/exploration only adjust it. A stable sort
    keeps ties in incoming order. Any failure returns the incoming order.
    """
    ordered = list(items)
    if not ordered:
        return []
    try:
        ids = [str(item.get("id")) for item in ordered if isinstance(item, dict)]
        stats = _stats(db, ids)
        n_total = sum(uses for uses, _ in stats.values())
        n_items = len(ordered)
        scored: list[tuple[float, int, dict]] = []
        for rank, item in enumerate(ordered):
            if similarity_key is None:
                similarity = 1.0 - rank / n_items
            else:
                similarity = 0.0
                if isinstance(item, dict):
                    try:
                        similarity = float(item.get(similarity_key) or 0.0)
                    except (TypeError, ValueError):
                        similarity = 0.0
            item_id = str(item.get("id")) if isinstance(item, dict) else ""
            uses, wins = stats.get(item_id, (0, 0))
            scored.append((score(similarity, uses, wins, n_total), rank, item))
        scored.sort(key=lambda entry: (-entry[0], entry[1]))
        return [entry[2] for entry in scored]
    except Exception:  # noqa: BLE001 — neutral value is the incoming order
        return list(items)
