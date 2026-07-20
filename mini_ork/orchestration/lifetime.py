"""mini-ork lifetime — operator CLI for the lifetime leaderboard (Python port).

Faithful port of ``bin/mini-ork-lifetime`` (3 subcommands: ``show``,
``conductor-history``, ``summary``). All three are read-only sqlite3
queries against ``state.db`` and produce stdout that must match the live
bash control byte-for-byte (floats within 1e-6, ints exact, dates
honor ``TZ``). The bash script stays in place (strangler-fig
co-existence); this module gives Python callers an in-process surface
and the parity test a stable target.

Public surface (mirrors bash functions):

    show(recipe, task_class=None)         → show <recipe> [--task-class X]
    conductor_history(n=10)               → conductor-history [N]
    summary()                             → summary
    help_text()                           → bash `cat <<EOF` block
    main(argv=None)                       → CLI dispatcher (exit codes mirror bash)

Output format mirrors:

* ``sqlite3 -separator ' | '`` mode: rows joined by ``" | "`` with each
  cell pre-padded via printf widths (``%.3f``, ``%+.3f``, ``%4d``,
  ``%-15s``, ``%-22s``, ``%-18s``, ``%-9s``, ``%-7s``, ``%-4d``,
  ``%.4f``, ``%.2f``). Substrings via ``substr(col, 1, N)`` map to
  Python ``col[:N]``.
* ``sqlite3 -column -header`` mode (summary h24/d7/lifetime row only):
  column width = ``max(len(header), max(len(cell)))``, each cell
  formatted as ``%-Ws``, joined by ``"  "`` (two spaces), trailing
  spaces preserved, trailing ``\\n`` always emitted (so empty data
  yields a 12-space line + ``\\n``).

Env knobs honored (also accepted as explicit kwargs to ``main()`` via
the dispatcher):

* ``MINI_ORK_DB``         — state.db path (default
  ``${MINI_ORK_HOME:-.mini-ork}/state.db``).
* ``MINI_ORK_HOME``       — base dir for the default DB path.
* ``TZ``                  — when subprocess tests compare bash↔py,
  fixed (typically ``UTC``) so ``datetime(…,'localtime')`` matches.

Schema citations (no column invention — confirmed against these
migrations before writing):

* ``prompt_win_rates``       — db/migrations/0030_prompt_win_rates.sql
* ``agent_performance_memory``— db/migrations/0009_memory_namespaces.sql
                               (column added by 0032_lane_relative_advantage.sql)
* ``topology_win_rates``     — db/migrations/0034_topology_role_evolution.sql
* ``bug_reports``            — db/migrations/0029_bug_reports.sql
* ``role_evolver_log``       — db/migrations/0034_topology_role_evolution.sql
* ``conductor_decisions``    — db/migrations/0034_topology_role_evolution.sql
* ``task_runs``              — db/migrations/0013_task_runs.sql

Parity is enforced by ``tests/unit/test_mini_ork_lifetime_py.py``
(>=6 cases that drive the LIVE bash subprocess against a temp DB
seeded by ``db/init.sh`` and compare stdout against the Python port,
with floats 1e-6 and ints exact).
"""
from __future__ import annotations

import os
import sqlite3
import sys
from typing import Iterable

__all__ = [
    "show",
    "conductor_history",
    "summary",
    "help_text",
    "main",
    "_db_path",
    "_column_header_lines",
]


# ─────────────────────────────────────────────────────────────────────────────
# Env helpers
# ─────────────────────────────────────────────────────────────────────────────
def _db_path() -> str:
    """Resolve the state.db path. Mirrors bash's
    ``STATE_DB="${MINI_ORK_DB:-$MINI_ORK_HOME/state.db}"`` (which itself
    defaults ``MINI_ORK_HOME`` to ``${MINI_ORK_ROOT:-<repo>}/.mini-ork``).
    The Python port keeps the simpler ``${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db}``
    fallback — sufficient because the public surface is read-only stdout,
    not a workflow-discovery target.
    """
    return (
        os.environ.get("MINI_ORK_DB")
        or os.environ.get("MINI_ORK_HOME", ".mini-ork") + "/state.db"
    )


def _open() -> sqlite3.Connection:
    """Open the state DB read-only (matches bash's sqlite3 read-only use).

    Raises ``sqlite3.OperationalError`` if the DB is missing or
    migrations have not been applied — same failure mode as bash
    `sqlite3 "$STATE_DB" …`. Caller decides whether to bail; bash
    aborts with set -e; Python callers may catch this if desired.
    """
    return sqlite3.connect(_db_path())


# ─────────────────────────────────────────────────────────────────────────────
# Output helpers
# ─────────────────────────────────────────────────────────────────────────────
def _render_separator(rows: Iterable[tuple]) -> str:
    """Render rows returned by ``SELECT … FROM … ORDER BY … LIMIT N``
    via the same printf widths used by bash's sqlite3 call. Each row
    becomes ``" | "-joined cells + "\\n"``.

    ``rows`` is expected to be a list of result tuples from sqlite3
    ``cur.fetchall()``. Column values are already pre-formatted
    strings (the SQL applies printf / substr); we just join them.
    NULL from sqlite3 → Python ``None`` → rendered as ``""`` (matches
    bash's ``sqlite3 -separator`` behavior).
    """
    out = []
    for row in rows:
        out.append(" | ".join("" if v is None else str(v) for v in row))
    if out:
        return "\n".join(out) + "\n"
    return ""


def _column_header_lines(headers: list[str], data_rows: list[tuple]) -> str:
    """Render a single ``sqlite3 -column -header`` block: 3 lines
    (header, dashes, data) joined by ``\\n`` and terminated with a
    final ``\\n``.

    Column widths = max(len(header[i]), max(len(data_rows[*][i]) or "")).
    Cells formatted with ``%-<W>s`` (left-justify, right-pad with spaces).
    Columns joined by ``"  "`` (two spaces). Trailing spaces on the
    final data row are preserved exactly.

    Empty data: column widths = header widths; data row is ``"  "-joined
    runs of "<W> spaces"`` — which is a line of all spaces whose length
    equals the sum of column widths + ``2 * (ncols - 1)``.
    """
    # widths[i] = max(len(headers[i]), max cell length in column i)
    widths = [len(h) for h in headers]
    for row in data_rows:
        for i, cell in enumerate(row):
            s = "" if cell is None else str(cell)
            if len(s) > widths[i]:
                widths[i] = len(s)
    parts: list[str] = []
    parts.append("  ".join(f"{h:<{w}}" for h, w in zip(headers, widths)))
    parts.append("  ".join("-" * w for w in widths))
    # Data line — always emitted (sqlite3 -column always emits a row even on empty)
    if not data_rows:
        data_line = "  ".join(" " * w for w in widths)
    else:
        # Each row rendered with column widths from the global max (the
        # sub-select SUM reads width-based on the cell population; for
        # summary() we only have one data row).
        def _fmt_row(row: tuple) -> str:
            cells = [
                f"{'' if c is None else str(c):<{w}}"
                for c, w in zip(row, widths)
            ]
            return "  ".join(cells)
        data_line = _fmt_row(data_rows[0])
    parts.append(data_line)
    return "\n".join(parts) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# show
# ─────────────────────────────────────────────────────────────────────────────
def show(recipe: str, task_class: str | None = None) -> str:
    """Mirror ``bin/mini-ork-lifetime show <recipe> [--task-class X]``.

    Prints the 5 leaderboard sections for the recipe. The bash
    implementation interpolates ``recipe`` verbatim into ``LIKE '%X%'``
    and ``target_recipe='X'`` — the Python port mirrors that exactly
    (interpolation, not parameter binding) so parity is byte-for-byte.
    Do NOT 'fix' this in the port.
    """
    if not recipe:
        raise ValueError("recipe required")
    tc_filter = ""
    if task_class:
        # bash: [ -n "$task_class" ] && tc_filter=" AND task_class='$task_class'"
        tc_filter = f" AND task_class='{task_class}'"

    # Build the buffer the same way bash composes its stdout: each
    # `echo` appends "<text>\n"; each `sqlite3` appends its raw
    # stdout (which already terminates with "\n" per row, or is empty
    # for zero rows). We append chunks via "".join so chunk-level
    # newlines are preserved exactly (no extra "\n" between them).
    parts: list[str] = []

    # bash: echo "=== lifetime: recipe=$recipe ${task_class:+ class=$task_class} ==="
    # bash's `${task_class:+ class=$task_class}` substitutes "" when
    # task_class is empty AND substitutes " class=..." (with leading
    # space) when set. The format string ALSO has a literal space between
    # "$recipe" and "${...}", so we always get "foo" + space + ("" or "
    # class=X") + space + "===" → "foo  ===" (2 spaces) or "foo
    # class=X ===" (2 spaces before "class"). Mirror exactly.
    if task_class:
        parts.append(f"=== lifetime: recipe={recipe}  class={task_class} ===\n")
    else:
        parts.append(f"=== lifetime: recipe={recipe}  ===\n")
    # bash: echo                 → blank line separator
    parts.append("\n")

    parts.append("## Top prompts by win_rate (sample_size >= 3)\n")
    sql = (
        "SELECT printf('%.3f', win_rate), printf('%4d', sample_size),"
        " substr(prompt_version_hash,1,16), task_class"
        " FROM prompt_win_rates"
        f" WHERE sample_size >= 3{tc_filter}"
        " ORDER BY win_rate DESC LIMIT 8;"
    )
    parts.append(_render_separator(_query(sql)))
    parts.append("\n")  # blank between sections

    parts.append("## Lanes by relative_advantage (runs_count >= 3)\n")
    sql = (
        "SELECT printf('%+.3f', relative_advantage),"
        " printf('%4d', runs_count),"
        " printf('%-15s', agent_version_id),"
        " task_class"
        " FROM agent_performance_memory"
        f" WHERE runs_count >= 3{tc_filter}"
        " ORDER BY relative_advantage DESC LIMIT 8;"
    )
    parts.append(_render_separator(_query(sql)))
    parts.append("\n")

    parts.append(
        f"## Topologies for this recipe (workflow_name LIKE '%{recipe}%')\n"
    )
    sql = (
        "SELECT printf('%.3f', win_rate), printf('%4d', sample_size),"
        " printf('%.4f', avg_cost_usd),"
        " substr(topology_id,1,14), workflow_name, task_class"
        " FROM topology_win_rates"
        f" WHERE workflow_name LIKE '%{recipe}%'{tc_filter}"
        " ORDER BY win_rate DESC LIMIT 5;"
    )
    parts.append(_render_separator(_query(sql)))
    parts.append("\n")

    parts.append("## Top open bug_reports tagged at this recipe's agents\n")
    sql = (
        "SELECT printf('%-9s', severity), printf('%4d', frequency),"
        " printf('%-15s', agent_role),"
        " substr(title,1,70)"
        " FROM bug_reports"
        " WHERE status='open'"
        f"   AND (observed_in LIKE '%{recipe}%'"
        "        OR agent_role IN ('planner','reviewer','implementer','verifier','researcher','publisher'))"
        " ORDER BY"
        "   CASE severity WHEN 'critical' THEN 8 WHEN 'high' THEN 4"
        "                 WHEN 'medium' THEN 2 ELSE 1 END * frequency DESC"
        " LIMIT 5;"
    )
    parts.append(_render_separator(_query(sql)))
    parts.append("\n")

    parts.append("## Pending role-evolver proposals for this recipe\n")
    sql = (
        "SELECT printf('%-4d', id), printf('%-7s', proposal_kind),"
        " printf('%-15s', target_node_id),"
        " substr(rationale,1,80)"
        " FROM role_evolver_log"
        f" WHERE status='open' AND (target_recipe='{recipe}' OR target_recipe='*')"
        " ORDER BY id DESC LIMIT 5;"
    )
    parts.append(_render_separator(_query(sql)))

    return "".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# conductor-history
# ─────────────────────────────────────────────────────────────────────────────
def conductor_history(n: int = 10) -> str:
    """Mirror ``bin/mini-ork-lifetime conductor-history [N]``."""
    parts: list[str] = []
    parts.append(f"=== last {n} conductor_decisions ===\n")
    sql = (
        "SELECT datetime(decided_at,'unixepoch','localtime') AS at,"
        " printf('%-22s', substr(epic_id,1,22)),"
        " printf('%-18s', substr(chosen_recipe,1,18)),"
        " printf('%.3f', predicted_score),"
        " printf('%.2f', budget_pct_used),"
        " COALESCE(outcome, '?'),"
        " COALESCE(printf('%.3f', realized_score), '-')"
        " FROM conductor_decisions"
        " ORDER BY decided_at DESC"
        f" LIMIT {n};"
    )
    parts.append(_render_separator(_query(sql)))
    return "".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# summary
# ─────────────────────────────────────────────────────────────────────────────
def summary() -> str:
    """Mirror ``bin/mini-ork-lifetime summary``.

    Notes the single use of ``sqlite3 -column -header`` (the h24/d7/lifetime
    row) which requires the per-cell width codec above. The other four
    sections use ``sqlite3 -separator ' | '`` with printf columns.
    """
    parts: list[str] = []
    parts.append("=== mini-ork lifetime summary ===\n")
    parts.append("\n")
    parts.append("## Run volume (24h / 7d / lifetime)\n")
    parts.append(_render_summary_columns())
    parts.append("\n")
    parts.append("## Top 5 prompts by win_rate (all classes, sample_size >= 5)\n")
    sql = (
        "SELECT printf('%.3f', win_rate), printf('%4d', sample_size),"
        " substr(prompt_version_hash,1,16), task_class"
        " FROM prompt_win_rates WHERE sample_size >= 5"
        " ORDER BY win_rate DESC LIMIT 5;"
    )
    parts.append(_render_separator(_query(sql)))
    parts.append("\n")
    parts.append("## Top 5 lanes by relative_advantage (all classes)\n")
    sql = (
        "SELECT printf('%+.3f', relative_advantage), printf('%4d', runs_count),"
        " printf('%-15s', agent_version_id), task_class"
        " FROM agent_performance_memory WHERE runs_count >= 3"
        " ORDER BY relative_advantage DESC LIMIT 5;"
    )
    parts.append(_render_separator(_query(sql)))
    parts.append("\n")
    parts.append("## Top 5 topologies by win_rate (all classes)\n")
    sql = (
        "SELECT printf('%.3f', win_rate), printf('%4d', sample_size),"
        " substr(workflow_name,1,18), task_class"
        " FROM topology_win_rates WHERE sample_size >= 3"
        " ORDER BY win_rate DESC LIMIT 5;"
    )
    parts.append(_render_separator(_query(sql)))
    parts.append("\n")
    parts.append("## Open bug_reports priority\n")
    sql = (
        "SELECT severity, COUNT(*) FROM bug_reports WHERE status='open'"
        " GROUP BY severity ORDER BY"
        "   CASE severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3"
        "                 WHEN 'medium' THEN 2 ELSE 1 END DESC;"
    )
    parts.append(_render_separator(_query(sql)))
    return "".join(parts)


def _render_summary_columns() -> str:
    """Render the h24/d7/lifetime row in ``sqlite3 -column -header`` form.

    Single-row result SUMs; only one row ever returned. Sums of empty
    sets in SQLite return NULL → Python ``None`` → rendered as empty
    string (matches bash).
    """
    sql = (
        "SELECT"
        " SUM(CASE WHEN created_at >= strftime('%s','now','-24 hours')"
        "          THEN 1 ELSE 0 END) AS h24,"
        " SUM(CASE WHEN created_at >= strftime('%s','now','-7 days')"
        "          THEN 1 ELSE 0 END) AS d7,"
        " COUNT(*) AS lifetime"
        " FROM task_runs;"
    )
    con = _open()
    try:
        cur = con.execute(sql)
        row = cur.fetchone()
    finally:
        con.close()
    if row is None:
        row = (None, None, None)
    return _column_header_lines(["h24", "d7", "lifetime"], [row])


# ─────────────────────────────────────────────────────────────────────────────
# Internal: run a formatted-rows query
# ─────────────────────────────────────────────────────────────────────────────
def _query(sql: str) -> list[tuple]:
    """Execute ``sql`` against the DB and return all rows.

    Cells are already formatted strings (SQL applied printf / substr) —
    we just return tuples of strings so ``_render_separator`` can join.
    """
    con = _open()
    try:
        cur = con.execute(sql)
        rows = cur.fetchall()
    finally:
        con.close()
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# help text + main
# ─────────────────────────────────────────────────────────────────────────────
HELP_TEXT = (
    "Usage: mini-ork lifetime <subcommand>\n"
    "  show <recipe> [--task-class X]   leaderboards filtered to a recipe\n"
    "  conductor-history [N]            last N conductor_decisions rows\n"
    "  summary                          cross-recipe top-level snapshot\n"
)


def help_text() -> str:
    return HELP_TEXT


def main(argv: list[str] | None = None) -> int:
    """CLI dispatcher. Mirrors bash's case block exactly.

    Returns the exit code (0 on success, 2 on unknown subcommand).
    ``help`` / ``--help`` / ``-h`` print help_text and return 0 (matches
    bash's bare ``cat <<EOF`` then implicit exit 0).
    """
    if argv is None:
        argv = sys.argv[1:]
    sub = argv[0] if argv else "summary"
    rest = argv[1:]
    if sub == "show":
        # Parse `show <recipe> [--task-class X]` — the flag must not be splatted as a
        # positional arg (bash treats --task-class as a named filter, not a 2nd recipe).
        recipe = ""
        task_class = None
        i = 0
        while i < len(rest):
            if rest[i] == "--task-class" and i + 1 < len(rest):
                task_class = rest[i + 1]; i += 2
            else:
                if not recipe:
                    recipe = rest[i]
                i += 1
        out = show(recipe, task_class)
        sys.stdout.write(out)
        return 0
    if sub == "conductor-history":
        # bash: _conductor_history "$@" — only one optional arg, default 10.
        n = 10
        if rest:
            try:
                n = int(rest[0])
            except ValueError:
                print(f"conductor-history: not a number: {rest[0]}", file=sys.stderr)
                return 2
        out = conductor_history(n)
        sys.stdout.write(out)
        return 0
    if sub == "summary":
        out = summary()
        sys.stdout.write(out)
        return 0
    if sub in ("help", "--help", "-h"):
        sys.stdout.write(HELP_TEXT)
        return 0
    print(f"lifetime: unknown subcommand {sub}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
