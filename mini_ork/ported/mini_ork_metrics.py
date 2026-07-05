"""Python port of bin/mini-ork-metrics — cross-cycle trajectory metrics.

Strangler-fig co-tenant: bash bin/mini-ork-metrics stays untouched; this port
gives Python callers an in-process target and tests a stable surface for
parity verification against the live bash.

    collect_cycles(state_db, recipe_filter='', since=None) -> dict
    render_json(data) -> str
    render_markdown(data) -> str
    main(argv=None) -> int

The bash script composes two embedded Python heredocs:
  1) collect_cycles: emits JSON {cycles, totals, since, recipe_filter}
  2) render_markdown: consumes that JSON and emits a markdown table

This port splits those into pure functions so tests can pin inputs/outputs.
Both renderers produce a single trailing '\\n' (bash `print(...)` and bash
`echo "$_data"` semantics).

Note on float f-strings: bash uses `:.4f` for cost_usd display, `:.1f` for
wall_min, `:.2f` for cost_delta, `+.1f` for wall_delta, `:.1f` for
trace_density. Any drift breaks byte-equality with the bash output.
"""
from __future__ import annotations

import datetime
import json
import os
import sqlite3
import sys
import time


def _busy_ms() -> int:
    raw = os.environ.get("MO_SQLITE_BUSY_MS", "5000")
    return int(raw)


def collect_cycles(state_db: str, recipe_filter: str = "", since=None) -> dict:
    """Mirror bash heredoc #1: query task_runs + execution_traces + gradient_records.

    Returns the dict the bash script prints as JSON:
        {cycles: [...], totals: {...}, since: <int>, recipe_filter: <str|'ALL'>}
    """
    con = sqlite3.connect(state_db)
    try:
        con.execute(f"PRAGMA busy_timeout={_busy_ms()}")

        clause = "WHERE created_at >= ?"
        params: list = [since]
        if recipe_filter:
            clause += " AND recipe = ?"
            params.append(recipe_filter)

        rows = con.execute(f"""
            SELECT id, recipe, status, cost_usd, created_at,
                   COALESCE(ended_at, strftime('%s','now')) AS ended_at_or_now
            FROM task_runs
            {clause}
            ORDER BY created_at ASC
        """, params).fetchall()

        trace_total = con.execute(
            "SELECT COUNT(*) FROM execution_traces "
            "WHERE CAST(strftime('%s', created_at) AS INTEGER) >= ?",
            (since,),
        ).fetchone()[0]

        try:
            grad_total = con.execute("SELECT COUNT(*) FROM gradient_records").fetchone()[0]
        except sqlite3.OperationalError:
            grad_total = 0
    finally:
        con.close()

    cycles: list[dict] = []
    for r in rows:
        rid, recipe, status, cost, created, ended = r
        cycles.append({
            "id": rid,
            "recipe": recipe or "",
            "status": status,
            "cost_usd": float(cost or 0),
            "created_at": int(created),
            "ended_at": int(ended) if ended else 0,
            "wall_secs": (int(ended) - int(created)) if ended else 0,
        })

    return {
        "cycles": cycles,
        "totals": {
            "cycle_count": len(cycles),
            "total_cost_usd": sum(c["cost_usd"] for c in cycles),
            "trace_count": trace_total,
            "gradient_count": grad_total,
        },
        "since": since,
        "recipe_filter": recipe_filter or "ALL",
    }


def render_json(data: dict) -> str:
    """Mirror bash `echo \"$_data\"` (one trailing newline)."""
    return json.dumps(data) + "\n"


def render_markdown(data: dict) -> str:
    """Mirror bash heredoc #2: markdown trajectory table.

    Output ends with a single '\\n' (last print() in bash heredoc).
    """
    cycles = data["cycles"]
    totals = data["totals"]
    out: list[str] = []

    out.append("# mini-ork trajectory")
    out.append("")
    out.append(f"**Recipe filter:** {data['recipe_filter']}  ")
    out.append(f"**Window:** since {datetime.datetime.fromtimestamp(data['since']).isoformat()}  ")
    out.append(f"**Cycles:** {totals['cycle_count']}  ")
    out.append(f"**Total cost:** ${totals['total_cost_usd']:.4f}  ")
    out.append(f"**Total traces:** {totals['trace_count']}  ")
    out.append(f"**Total gradients:** {totals['gradient_count']}")
    out.append("")

    if not cycles:
        out.append("_No cycles in window._")
        return "\n".join(out) + "\n"

    out.append("| # | Run ID | Recipe | Status | Cost $ | Wall (min) | Created |")
    out.append("|---|--------|--------|--------|---------|------------|---------|")
    for i, c in enumerate(cycles, 1):
        wall_min = c["wall_secs"] / 60.0
        created = datetime.datetime.fromtimestamp(c["created_at"]).strftime("%Y-%m-%d %H:%M")
        out.append(f"| {i} | `{c['id'][:24]}` | {c['recipe']} | {c['status']} | {c['cost_usd']:.4f} | {wall_min:.1f} | {created} |")

    out.append("")
    out.append("## Trajectory signal")
    out.append("")
    if len(cycles) >= 2:
        first, last = cycles[0], cycles[-1]
        cost_delta = last["cost_usd"] - first["cost_usd"]
        wall_delta = (last["wall_secs"] - first["wall_secs"]) / 60.0
        out.append(f"- Cost trend: first ${first['cost_usd']:.2f} → last ${last['cost_usd']:.2f} (Δ ${cost_delta:+.2f})")
        out.append(f"- Wall trend: first {first['wall_secs']/60:.1f}min → last {last['wall_secs']/60:.1f}min (Δ {wall_delta:+.1f}min)")
    out.append(f"- Trace density: {totals['trace_count']/max(totals['cycle_count'],1):.1f} traces/cycle avg")
    out.append(f"- Gradient yield: {totals['gradient_count']} gradients across {totals['cycle_count']} cycles")

    return "\n".join(out) + "\n"


def _usage() -> str:
    return (
        "Usage: mini-ork metrics [--recipe <name>] [--since <epoch>] [--format markdown|json]\n"
        "\n"
        "Emit cross-cycle trajectory metrics from state.db. Reads task_runs +\n"
        "execution_traces + gradient_records to show the framework's self-\n"
        "improvement signal over time.\n"
        "\n"
        "Options:\n"
        "  --recipe <name>      Filter to one recipe (e.g. refactor-audit)\n"
        "  --since <epoch>      Unix timestamp lower bound (default: 7 days ago)\n"
        "  --format markdown    Markdown table (default; pretty-prints to terminal)\n"
        "  --format json        JSON array (pipeable, e.g. mini-ork metrics --format json | jq ...)\n"
        "  --help               This message\n"
        "\n"
        "Phase C deliverable. Trajectory measurement enables the framework's\n"
        "\"measurable improvement\" claim — without this, claims of self-\n"
        "improvement are narrative not numeric.\n"
    )


def _parse(argv):
    """Returns (recipe, since, fmt, help_flag, unknown_flag, error)."""
    if argv is None:
        argv = sys.argv[1:]
    recipe = ""
    since: int | None = None
    fmt = "markdown"
    for a in argv:
        if a in ("--help", "-h"):
            return recipe, since, fmt, True, None, None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--recipe":
            if i + 1 >= len(argv):
                return recipe, since, fmt, False, None, "missing value for --recipe"
            recipe = argv[i + 1]
            i += 2
        elif a == "--since":
            if i + 1 >= len(argv):
                return recipe, since, fmt, False, None, "missing value for --since"
            try:
                since = int(argv[i + 1])
            except ValueError:
                return recipe, since, fmt, False, None, f"invalid epoch for --since: {argv[i+1]}"
            i += 2
        elif a == "--format":
            if i + 1 >= len(argv):
                return recipe, since, fmt, False, None, "missing value for --format"
            fmt = argv[i + 1]
            if fmt not in ("markdown", "json"):
                return recipe, since, fmt, False, None, f"invalid --format: {fmt}"
            i += 2
        else:
            return recipe, since, fmt, False, a, None
    return recipe, since, fmt, False, None, None


def main(argv=None, *, stdout=None, stderr=None) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    recipe, since, fmt, help_flag, unknown, parse_err = _parse(argv)
    if help_flag:
        out.write(_usage())
        return 0
    if parse_err:
        err.write(f"{parse_err}\n")
        out.write(_usage())
        return 2
    if unknown is not None:
        err.write(f"Unknown flag: {unknown}\n")
        out.write(_usage())
        return 2

    state_db = os.environ.get("MINI_ORK_DB", "")
    if not state_db:
        err.write("no state.db (MINI_ORK_DB not set)\n")
        return 1
    if not os.path.isfile(state_db):
        err.write(f"no state.db at {state_db}\n")
        return 1

    if since is None:
        since = int(time.time()) - 7 * 86400

    data = collect_cycles(state_db, recipe_filter=recipe, since=since)
    if fmt == "json":
        out.write(render_json(data))
    else:
        out.write(render_markdown(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())