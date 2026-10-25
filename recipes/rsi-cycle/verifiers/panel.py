#!/usr/bin/env python3
"""Grade the panel that actually served, from `llm_calls`.

The node declarations *ask* for four families. This node reports what answered:
a lane can fall back to another provider mid-run, and a panel that quietly
became one family is exactly the failure the four-lane layout exists to avoid.

Aliases are read back out of `llm_calls` for this run rather than passed in,
because the alias the engine records is the one the check has to grade — passing
a list we guessed would manufacture `no_calls` problems on a healthy run.

Fail-open when there is nothing to grade (no DB, no table, no rows), matching
`check_panel`'s own contract: an unmeasured panel is reported as skipped, never
as healthy.
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import write_json

STAGE = "panel"


def _served_actors(db: str, run_id: str) -> list[str] | None:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        rows = con.execute(
            "SELECT DISTINCT actor FROM llm_calls WHERE CAST(run_id AS TEXT) = ? "
            "AND actor IS NOT NULL AND actor <> ''",
            (str(run_id),),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return sorted({str(row[0]) for row in rows})


def main() -> None:
    run_id = os.environ.get("MINI_ORK_RUN_ID", "").strip()
    db = os.environ.get("MINI_ORK_DB", "").strip()
    result = {"ok": True, "problems": [], "skipped": None, "served": {}, "families": {}}
    actors = _served_actors(db, run_id) if (db and run_id and os.path.isfile(db)) else None
    if actors is None:
        result["skipped"] = "no usable llm_calls table"
    elif not actors:
        result["skipped"] = f"no llm_calls rows for run {run_id!r}"
    else:
        from mini_ork.verify.panel_heterogeneity import check_panel

        result = check_panel(run_id, actors, db=db)

    write_json("panel-composition.json", result, "rsi-cycle-panel")
    if not result.get("ok", True):
        sys.stderr.write(f"{STAGE}: panel problems: {', '.join(result.get('problems') or [])}\n")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
