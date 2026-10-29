"""``mini-ork metrics revise`` — rounds-to-pass, attempt history, and the
reviewer findings ledger, read-only.

Three sections:

- ``revise_rounds`` — per task class over the last ``--days``: how many runs,
  how many passed on their first attempt, the distribution of the smallest
  passing round per ``(run_id, target_node)``, and how many failed or are
  still open (their max-round row has ``passed = NULL``).
- ``history`` — implementer attempts per run, from
  ``node_attempts`` (``MAX(attempt_no)`` for ``node_id='implementer'``) joined
  to ``task_runs.task_class``. Omitted when either table is missing.
- ``convergence`` — the R5a review ledger (``<run>/revise/findings.json``)
  summed across run dirs modified within the window: how many findings were
  new, reported again, fixed, reopened, or outside the revision delta.
  Omitted when the runs dir does not exist.

The DB is never written: it is opened through
:func:`mini_ork.cli.metrics_sdd.connect_readonly`. A missing table degrades to
an empty/omitted section rather than an error.

Note: ``passed`` is run-level, not target-level — the executor settles it from
its global failure count — so ``passed_first_try`` means "the run was green on
its first attempt", not "this target's own gate passed".
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from mini_ork.cli.metrics_sdd import _default_db, connect_readonly

CONVERGENCE_KEYS = ("new", "reported_again", "fixed", "still_open",
                    "reopened", "outside_delta")


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


# --------------------------------------------------------------- revise rounds


def _summarize(rows) -> dict:
    """One task class's rounds, from ``revise_rounds`` rows in the window."""
    runs = {r["run_id"] for r in rows}
    by_pair: dict[tuple[str, str], list] = defaultdict(list)
    by_run: dict[str, list] = defaultdict(list)
    for r in rows:
        by_pair[(r["run_id"], r["target_node"])].append(r)
        by_run[r["run_id"]].append(r)

    passed_first_try: set[str] = set()
    rounds_to_pass: Counter[int] = Counter()
    for (run_id, _target), pair_rows in by_pair.items():
        first = next((r for r in pair_rows if r["round"] == 0), None)
        if first is not None and first["passed"] == 1:
            passed_first_try.add(run_id)
        passing = [r["round"] for r in pair_rows if r["passed"] == 1]
        if passing:
            rounds_to_pass[min(passing)] += 1

    failed: set[str] = set()
    open_runs: set[str] = set()
    for run_id, run_rows in by_run.items():
        max_round = max(r["round"] for r in run_rows)
        final = [r for r in run_rows if r["round"] == max_round]
        if any(r["passed"] is None for r in final):
            open_runs.add(run_id)
        elif not any(r["passed"] == 1 for r in run_rows):
            failed.add(run_id)

    return {
        "runs": len(runs),
        "passed_first_try": len(passed_first_try),
        "rounds_to_pass": dict(sorted(rounds_to_pass.items())),
        "failed": len(failed),
        "open": len(open_runs),
    }


def collect_revise_rounds(con, tables: set[str], since: int,
                          task_class: str | None) -> dict:
    if "revise_rounds" not in tables:
        return {}
    sql = (
        "SELECT run_id, task_class, target_node, round, passed"
        " FROM revise_rounds WHERE created_at >= ?"
    )
    params: list = [since]
    if task_class:
        sql += " AND task_class = ?"
        params.append(task_class)
    try:
        rows = con.execute(sql, params).fetchall()
    except sqlite3.Error:
        return {}
    grouped: dict[str, list] = defaultdict(list)
    for r in rows:
        grouped[r["task_class"] if r["task_class"] is not None else "unknown"].append(r)
    return {cls: _summarize(cls_rows) for cls, cls_rows in sorted(grouped.items())}


# ------------------------------------------------------------- attempt history


def collect_history(con, tables: set[str], since: int) -> dict | None:
    """Implementer attempts per run, or ``None`` when a table is missing."""
    if not {"node_attempts", "task_runs"} <= tables:
        return None
    try:
        rows = con.execute(
            "SELECT t.task_class AS task_class,"
            "       a.run_id AS run_id,"
            "       MAX(a.attempt_no) AS attempts"
            " FROM node_attempts a"
            " JOIN task_runs t ON t.id = a.run_id"
            " WHERE a.node_id = 'implementer' AND t.created_at >= ?"
            " GROUP BY a.run_id, t.task_class",
            (since,),
        ).fetchall()
    except sqlite3.Error:
        return None
    by_class: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        by_class[r["task_class"] if r["task_class"] is not None else "unknown"][
            int(r["attempts"])
        ] += 1
    return {
        "label": "implementer attempts per run (node_attempts)",
        "by_task_class": {
            cls: {str(n): count for n, count in sorted(counter.items())}
            for cls, counter in sorted(by_class.items())
        },
    }


# ---------------------------------------------------------------- convergence


def collect_convergence(runs_dir: Path, since: int) -> dict | None:
    """Sum the reviewer findings ledger across recently modified run dirs."""
    if not runs_dir.is_dir():
        return None
    sums = {key: 0 for key in CONVERGENCE_KEYS}
    runs_with_ledger = 0
    try:
        entries = sorted(runs_dir.iterdir())
    except OSError:
        return None
    for run_dir in entries:
        try:
            if not run_dir.is_dir() or run_dir.stat().st_mtime < since:
                continue
        except OSError:
            continue
        ledger = run_dir / "revise" / "findings.json"
        if not ledger.is_file():
            continue
        try:
            data = json.loads(ledger.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rounds = data.get("rounds") if isinstance(data, dict) else None
        if not isinstance(rounds, list):
            continue
        for rec in rounds:
            if not isinstance(rec, dict):
                continue
            for key in CONVERGENCE_KEYS:
                value = rec.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    sums[key] += value
        runs_with_ledger += 1
    return {"runs_with_ledger": runs_with_ledger, **sums}


# --------------------------------------------------------------------- output


def collect(db: str, *, days: int, task_class: str | None,
            runs_dir: Path) -> dict:
    since = _window_since(days)
    con = connect_readonly(db)
    try:
        tables = _tables(con)
        data = {
            "revise_rounds": collect_revise_rounds(con, tables, since, task_class),
            "window_days": int(days),
        }
        history = collect_history(con, tables, since)
    finally:
        con.close()
    if history is not None:
        data["history"] = history
    convergence = collect_convergence(runs_dir, since)
    if convergence is not None:
        data["convergence"] = convergence
    return data


def _histogram_text(hist: dict) -> str:
    if not hist:
        return "—"
    ordered = sorted(hist.items(), key=lambda item: int(item[0]))
    return ", ".join(f"{rounds}: {count}" for rounds, count in ordered)


def render_markdown(data: dict) -> str:
    lines = [
        "# revise rounds to pass",
        "",
        f"Window: last {data['window_days']} days.",
        "",
        "## revise rounds",
        "",
        "| Task class | Runs | Passed first try | Failed | Open | Rounds to pass |",
        "|---|---:|---:|---:|---:|---|",
    ]
    revise = data.get("revise_rounds") or {}
    if revise:
        for cls, m in sorted(revise.items()):
            lines.append(
                f"| {cls} | {m['runs']} | {m['passed_first_try']} | "
                f"{m['failed']} | {m['open']} | {_histogram_text(m['rounds_to_pass'])} |"
            )
    else:
        lines.append("| *(no rows)* | 0 | 0 | 0 | 0 | — |")

    history = data.get("history")
    if history is not None:
        lines += [
            "",
            "## implementer attempts per run (node_attempts)",
            "",
            "| Task class | Attempts per run |",
            "|---|---|",
        ]
        by_class = history.get("by_task_class") or {}
        if by_class:
            for cls, hist in sorted(by_class.items()):
                lines.append(f"| {cls} | {_histogram_text(hist)} |")
        else:
            lines.append("| *(no rows)* | — |")

    convergence = data.get("convergence")
    if convergence is not None:
        lines += [
            "",
            "## reviewer convergence",
            "",
            "| Runs with ledger | New | Reported again | Fixed | Still open | Reopened | Outside delta |",
            "|---:|---:|---:|---:|---:|---:|---:|",
            f"| {convergence['runs_with_ledger']} | {convergence['new']} | "
            f"{convergence['reported_again']} | {convergence['fixed']} | "
            f"{convergence['still_open']} | {convergence['reopened']} | "
            f"{convergence['outside_delta']} |",
        ]

    lines += [
        "",
        "`passed` is the run's own gate, not the target's: `passed_first_try` means",
        "the run was green on its first attempt.",
        "",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """The argv contract, shared by ``main`` and any in-process caller."""
    parser = argparse.ArgumentParser(
        prog="mini-ork metrics revise",
        description="Rounds-to-pass, implementer attempts, and the reviewer findings ledger.",
    )
    parser.add_argument("--db", default=None,
                        help="state.db (default: $MINI_ORK_DB, else <home>/state.db)")
    parser.add_argument("--days", type=int, default=30,
                        help="window in days (default: 30)")
    parser.add_argument("--task-class", default=None,
                        help="only rows for this task class")
    parser.add_argument("--runs-dir", default=None,
                        help="run dirs to scan (default: next to the DB)")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None, *, stdout=None, stderr=None) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    a = build_parser().parse_args(argv)
    db = a.db or _default_db()
    if not os.path.isfile(db):
        err.write(f"mini-ork metrics revise: no state.db at {db}\n")
        return 1
    runs_dir = Path(a.runs_dir) if a.runs_dir else Path(db).resolve().parent / "runs"
    data = collect(db, days=a.days, task_class=a.task_class, runs_dir=runs_dir)
    if a.json:
        out.write(json.dumps(data, sort_keys=True) + "\n")
    else:
        out.write(render_markdown(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
