#!/usr/bin/env python3
"""Backfill idea_tree_nodes from existing self_improve_runs history.

Strategy (chronological-linear per day-cluster):
  1. Group self_improve_runs by date(started_at, 'unixepoch'). Each day
     becomes one loop session.
  2. For each session, create a synthetic root node (no task_run_id, no
     self_improve_run_id — it's just a frame for the objective).
  3. Within each session, order rows by started_at ASC and assign
     parent_node_id = the previous iter's node_id. iter 1's parent is the
     synthetic root.
  4. Map self_improve_runs.outcome to idea_tree_nodes.status:
        success / converged → harvested
        rejected / failed / aborted / timed_out → pruned
        partial / pending → pending

Why a separate root per day instead of one global root:
  Each day's self-improve session was a distinct goal (operators picked a
  fresh kickoff). Lumping them under a global root would imply they share
  ancestry semantics, which is false. Inspect docs/plans/
  2026-06-11-arbor-techniques-into-mini-ork.md for the design rationale.

This is idempotent: running twice produces no duplicate nodes (uses
INSERT OR IGNORE keyed on a deterministic node_id derived from
self_improve_run_id).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path


# Outcome → idea_tree_node status mapping.
OUTCOME_TO_STATUS = {
    "success": "harvested",
    "converged": "harvested",
    "partial": "pending",   # ambiguous; leave open for the loop to revisit
    "pending": "pending",
    "rejected": "pruned",
    "failed": "pruned",
    "aborted": "pruned",
    "timed_out": "pruned",
}


def _node_id_for_iter(self_improve_run_id: str) -> str:
    """Deterministic node_id from self_improve_run_id (re-run safe)."""
    return f"itn-self-improve-{self_improve_run_id}"


def _synthetic_root_id(day: str, recipe: str | None) -> str:
    """Deterministic root id per (day, recipe) cluster."""
    suffix = recipe or "unknown"
    return f"itn-root-{day}-{suffix}"


def backfill(db_path: Path, dry_run: bool = False) -> dict[str, int]:
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    cur = con.cursor()

    # Confirm the migration has run.
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='idea_tree_nodes'"
    )
    if not cur.fetchone():
        print(
            "ERROR: idea_tree_nodes table missing. Apply db/migrations/0020_idea_tree.sql first.",
            file=sys.stderr,
        )
        return {"rc": 1}

    # Pull self_improve_runs ordered by start time.
    rows = cur.execute(
        """
        SELECT run_id, started_at, iter, outcome, notes,
               date(started_at, 'unixepoch') AS day
        FROM self_improve_runs
        ORDER BY started_at ASC, iter ASC
        """
    ).fetchall()

    if not rows:
        print("no self_improve_runs to backfill")
        return {"total_iters": 0, "synthetic_roots": 0, "tree_nodes": 0}

    # Group by day. Each day = one session = one synthetic root.
    by_day: dict[str, list[tuple]] = defaultdict(list)
    for row in rows:
        by_day[row[5]].append(row)

    synthetic_roots = 0
    tree_nodes = 0
    now = int(time.time())

    for day, day_rows in by_day.items():
        # Heuristic: all day_rows came from the same recipe (recursive-self-improve
        # is the only one that writes self_improve_runs today). Hard-code for
        # now; if multiple recipes start writing here, this groups under the
        # first one and the loop can be expanded.
        recipe = "recursive-self-improve"
        root_id = _synthetic_root_id(day, recipe)

        # Insert synthetic root (idempotent). In dry-run, check existence
        # explicitly so the counter reflects what WOULD be inserted.
        root_exists = cur.execute(
            "SELECT 1 FROM idea_tree_nodes WHERE node_id = ?",
            (root_id,),
        ).fetchone() is not None
        if dry_run:
            if not root_exists:
                synthetic_roots += 1
        else:
            cur.execute(
                """
                INSERT OR IGNORE INTO idea_tree_nodes
                    (node_id, parent_node_id, root_node_id, recipe,
                     task_run_id, self_improve_run_id, hypothesis,
                     status, insights_json, created_at, updated_at)
                VALUES (?, NULL, ?, ?, NULL, NULL, ?,
                        'harvested', '[]', ?, ?)
                """,
                (
                    root_id,
                    root_id,
                    recipe,
                    f"Self-improve session on {day}: {len(day_rows)} iters",
                    now,
                    now,
                ),
            )
            if cur.rowcount == 1:
                synthetic_roots += 1

        # Walk day's rows, chronological-linear parenting.
        prev_node_id = root_id
        for (run_id, started_at, iter_num, outcome, notes, _) in day_rows:
            node_id = _node_id_for_iter(run_id)
            status = OUTCOME_TO_STATUS.get(outcome, "pending")
            hypothesis = (
                f"iter {iter_num}"
                if not notes
                else f"iter {iter_num}: {notes[:120]}"
            )

            node_exists = cur.execute(
                "SELECT 1 FROM idea_tree_nodes WHERE node_id = ?",
                (node_id,),
            ).fetchone() is not None
            if dry_run:
                if not node_exists:
                    tree_nodes += 1
            else:
                cur.execute(
                    """
                    INSERT OR IGNORE INTO idea_tree_nodes
                        (node_id, parent_node_id, root_node_id, recipe,
                         task_run_id, self_improve_run_id, hypothesis,
                         status, insights_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?)
                    """,
                    (
                        node_id,
                        prev_node_id,
                        root_id,
                        recipe,
                        run_id,        # task_run_id — the run_id IS the task_run id for self-improve
                        run_id,        # self_improve_run_id
                        hypothesis,
                        status,
                        started_at or now,
                        now,
                    ),
                )
                if cur.rowcount == 1:
                    tree_nodes += 1

            prev_node_id = node_id

    if not dry_run:
        con.commit()

    con.close()
    return {
        "total_iters": len(rows),
        "synthetic_roots": synthetic_roots,
        "tree_nodes": tree_nodes,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--db",
        type=Path,
        default=Path(".mini-ork/state.db"),
        help="Path to state.db (default: .mini-ork/state.db)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute what would be inserted, write nothing",
    )
    args = p.parse_args()

    if not args.db.exists():
        print(f"db not found: {args.db}", file=sys.stderr)
        return 1

    out = backfill(args.db, dry_run=args.dry_run)
    if "rc" in out:
        return out["rc"]

    label = "would insert" if args.dry_run else "inserted"
    print(
        f"backfill_idea_tree: scanned {out['total_iters']} iters; "
        f"{label} {out['synthetic_roots']} synthetic roots + "
        f"{out['tree_nodes']} tree nodes"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
