"""``mini-ork metrics explore`` — attempt spend by move kind, read-only.

``node_attempts`` records every node attempt. Migration 0093 adds
``move_kind``, which says what KIND of move an attempt was: the node's
``initial`` attempt, a ``retry`` forced by infrastructure, a ``revise`` that
took feedback, a ``recover`` re-execution after an interrupted run, or an
``explore`` that deliberately tried a different idea.

That distinction is the whole point of the command. A cost review that only
sees "attempts" cannot tell convergence spending (revise) from waste spending
(retry) from the spending that is supposed to buy a better idea (explore);
this table can. Two caveats are printed with it, because both would otherwise
become false conclusions:

- ``explore`` is NEVER inferred — only a dispatcher that declared it produces
  it. A ``0`` in that column means "nobody declared a different-idea attempt",
  not "the agent never explored".
- rows written before 0093 are ``unclassified`` and are counted as such. The
  headline therefore reports how much of the attempt population is
  unclassified, so a mostly-NULL table reads as "not measured yet" instead of
  as a small revise share.

The DB is never written: it is opened through
:func:`mini_ork.cli.metrics_sdd.connect_readonly`. A missing table or an
un-migrated ``node_attempts`` degrades to an omitted section with a reason.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict

from mini_ork.cli.metrics_sdd import _default_db, connect_readonly

#: Accounting bucket for rows that predate migration 0093.
UNCLASSIFIED = "unclassified"

MOVE_ORDER = ("initial", "revise", "explore", "retry", "recover", UNCLASSIFIED)


def _window_since(days: int) -> int:
    return int(time.time()) - int(days) * 86400


def _tables(con: sqlite3.Connection) -> set[str]:
    try:
        return {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    except sqlite3.Error:
        return set()


def _has_move_kind(con: sqlite3.Connection) -> bool:
    """True when this DB's ``node_attempts`` carries migration 0093's column."""
    try:
        rows = con.execute("PRAGMA table_info(node_attempts)").fetchall()
    except sqlite3.Error:
        return False
    return any(len(row) > 1 and row[1] == "move_kind" for row in rows)


def _share(part: float, whole: float) -> float:
    return (part / whole) if whole else 0.0


# ------------------------------------------------------------------- collect


def collect_moves(con, tables: set[str], since: int,
                  task_class: str | None) -> tuple[dict | None, str]:
    """Attempt counts, successes and cost per ``(task class, move kind)``.

    Returns ``({...}, "")`` or ``(None, reason)`` when the data cannot be
    measured — an omitted section, never a table of zeros that reads as a
    healthy run.
    """
    if "node_attempts" not in tables:
        return None, "node_attempts table not present"
    if not _has_move_kind(con):
        return None, (
            "node_attempts.move_kind not migrated "
            "(apply db/migrations/0093_node_attempts_move_kind.sql)"
        )
    # ``task_runs`` is optional: a state.db with the ledger but no run table
    # still measures, it just cannot attribute attempts to a task class.
    join = ""
    class_field = "'unknown' AS task_class"
    if "task_runs" in tables:
        join = " JOIN task_runs t ON t.id = a.run_id"
        class_field = "COALESCE(t.task_class, 'unknown') AS task_class"
    sql = (
        f"SELECT {class_field},"
        "       COALESCE(a.move_kind, ?) AS move_kind,"
        "       COUNT(*) AS attempts,"
        "       COALESCE(SUM(CASE WHEN a.result = 'success'"
        "                         THEN 1 ELSE 0 END), 0) AS successes,"
        "       COALESCE(SUM(a.cost_usd), 0.0) AS cost_usd"
        f" FROM node_attempts a{join}"
        " WHERE a.ended_at >= ?"
        " GROUP BY 1, 2"
    )
    params: list = [UNCLASSIFIED, since]
    if task_class:
        sql = sql.replace(
            " WHERE a.ended_at >= ?",
            " WHERE a.ended_at >= ? AND COALESCE(t.task_class, 'unknown') = ?",
        )
        params.append(task_class)
    try:
        rows = con.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        return None, f"node_attempts could not be read: {exc}"

    per_class: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in rows:
        cls = r["task_class"] if r["task_class"] is not None else "unknown"
        kind = r["move_kind"] or UNCLASSIFIED
        per_class[cls][kind] = {
            "attempts": int(r["attempts"] or 0),
            "successes": int(r["successes"] or 0),
            "cost_usd": float(r["cost_usd"] or 0.0),
        }

    total_attempts = total_successes = 0
    total_cost = 0.0
    cost_by_kind: dict[str, float] = defaultdict(float)
    attempts_by_kind: dict[str, int] = defaultdict(int)
    by_task_class: dict[str, dict] = {}
    for cls, kinds in sorted(per_class.items()):
        cls_attempts = sum(v["attempts"] for v in kinds.values())
        cls_cost = sum(v["cost_usd"] for v in kinds.values())
        total_attempts += cls_attempts
        total_cost += cls_cost
        entry: dict[str, dict] = {}
        for kind, v in kinds.items():
            total_successes += v["successes"]
            cost_by_kind[kind] += v["cost_usd"]
            attempts_by_kind[kind] += v["attempts"]
            entry[kind] = {
                **v,
                "share_of_attempts": _share(v["attempts"], cls_attempts),
                "share_of_cost": _share(v["cost_usd"], cls_cost),
                "success_rate": _share(v["successes"], v["attempts"]),
            }
        by_task_class[cls] = entry

    headline = {
        "attempts": total_attempts,
        "successes": total_successes,
        "cost_usd": total_cost,
        # How many attempts each accepted attempt cost. ``None`` rather than a
        # division by zero when nothing succeeded in the window.
        "attempts_per_success": (
            _share(total_attempts, total_successes) if total_successes else None
        ),
        "share_of_cost": {
            kind: _share(cost_by_kind.get(kind, 0.0), total_cost)
            for kind in MOVE_ORDER
        },
        "share_of_attempts": {
            kind: _share(attempts_by_kind.get(kind, 0), total_attempts)
            for kind in MOVE_ORDER
        },
    }
    return {
        "label": "attempts by move kind (node_attempts.move_kind)",
        "by_task_class": by_task_class,
        "headline": headline,
    }, ""


def collect(db: str, *, days: int, task_class: str | None) -> dict:
    since = _window_since(days)
    con = connect_readonly(db)
    try:
        tables = _tables(con)
        moves, reason = collect_moves(con, tables, since, task_class)
    finally:
        con.close()
    data: dict = {"window_days": int(days)}
    if moves is None:
        data["moves"] = None
        data["unavailable"] = reason
    else:
        data["moves"] = moves
    return data


# -------------------------------------------------------------------- render


def _pct(x: float) -> str:
    return f"{100.0 * x:.0f}%"


def _kinds_in_order(kinds) -> list[str]:
    known = [k for k in MOVE_ORDER if k in kinds]
    return known + sorted(k for k in kinds if k not in MOVE_ORDER)


def render_markdown(data: dict) -> str:
    lines = [
        "# attempt move kinds",
        "",
        f"Window: last {data['window_days']} days.",
        "",
    ]
    moves = data.get("moves")
    if moves is None:
        lines += [
            "## attempts by move kind",
            "",
            f"Not measurable: {data.get('unavailable', 'unknown reason')}.",
            "",
        ]
        return "\n".join(lines)

    lines += [
        "## attempts by move kind",
        "",
        "| Task class | Move | Attempts | Successes | Success rate "
        "| Share of attempts | Share of cost | Cost (USD) |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    by_class = moves.get("by_task_class") or {}
    if by_class:
        for cls, kinds in sorted(by_class.items()):
            for kind in _kinds_in_order(kinds):
                v = kinds[kind]
                lines.append(
                    f"| {cls} | {kind} | {v['attempts']} | {v['successes']} | "
                    f"{_pct(v['success_rate'])} | {_pct(v['share_of_attempts'])} | "
                    f"{_pct(v['share_of_cost'])} | {v['cost_usd']:.4f} |"
                )
    else:
        lines.append("| *(no attempts in window)* | — | 0 | 0 | — | — | — | 0.0 |")

    h = moves.get("headline") or {}
    aps = h.get("attempts_per_success")
    share_cost = h.get("share_of_cost") or {}
    share_att = h.get("share_of_attempts") or {}
    lines += [
        "",
        "## headline",
        "",
        f"- attempts: {h.get('attempts', 0)}, successes: {h.get('successes', 0)}, "
        f"cost: {h.get('cost_usd', 0.0):.4f} USD",
        "- attempts per accepted attempt: "
        + ("— (nothing succeeded)" if aps is None else f"{aps:.2f}"),
        f"- revise: {_pct(share_cost.get('revise', 0.0))} of cost, "
        f"{_pct(share_att.get('revise', 0.0))} of attempts",
        f"- retry: {_pct(share_cost.get('retry', 0.0))} of cost, "
        f"{_pct(share_att.get('retry', 0.0))} of attempts",
        f"- explore: {_pct(share_cost.get('explore', 0.0))} of cost, "
        f"{_pct(share_att.get('explore', 0.0))} of attempts",
        f"- unclassified: {_pct(share_att.get(UNCLASSIFIED, 0.0))} of attempts",
        "",
        "`explore` is caller-declared only — it is never inferred, so a zero",
        "means no dispatcher claimed a different-idea attempt, not that none",
        "was possible. `unclassified` counts rows written before migration",
        "0093 (or with the column absent); while it is large, the other rows",
        "describe a sample, not the population.",
        "",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """The argv contract, shared by ``main`` and any in-process caller."""
    parser = argparse.ArgumentParser(
        prog="mini-ork metrics explore",
        description="Attempt spend by move kind (initial/retry/revise/recover/explore).",
    )
    parser.add_argument("--db", default=None,
                        help="state.db (default: $MINI_ORK_DB, else <home>/state.db)")
    parser.add_argument("--days", type=int, default=30,
                        help="window in days (default: 30)")
    parser.add_argument("--task-class", default=None,
                        help="only rows for this task class")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None, *, stdout=None, stderr=None) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    a = build_parser().parse_args(argv)
    db = a.db or _default_db()
    if not os.path.isfile(db):
        err.write(f"mini-ork metrics explore: no state.db at {db}\n")
        return 1
    data = collect(db, days=a.days, task_class=a.task_class)
    if a.json:
        out.write(json.dumps(data, sort_keys=True) + "\n")
    else:
        out.write(render_markdown(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
