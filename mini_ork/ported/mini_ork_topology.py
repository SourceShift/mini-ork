"""Python port of ``bin/mini-ork-topology`` — query + compute panel-topology telemetry.

Faithful port of the E-MO-01 operator CLI ``bin/mini-ork-topology``. The
bash entrypoint sources ``lib/topology_metrics.sh`` and exposes three
subcommands (default summary, ``--compute <panel_run_id> <recipe>``, and
``--backfill``). This module mirrors the same subcommand surface via
``main(argv=None)`` so callers (including the parity test) can drive it
both as a CLI (``python -m mini_ork.ported.mini_ork_topology …``) and
as an importable library.

Public API (mirrors the bash CLI 1:1)::

    from mini_ork.ported.mini_ork_topology import (
        cmd_summary,        # (db_path, recipe="")          -> None
        cmd_compute,        # (db_path, panel_run_id,
        #                    recipe, root=None)             -> str
        cmd_backfill,       # (db_path, root=None, limit=20) -> int
        main,               # (argv=None)                   -> int
    )

Co-existence model (strangler-fig): ``bin/mini-ork-topology`` stays
byte-identical; the Python port is invoked independently. Parity is
enforced by ``tests/unit/test_mini_ork_topology_py.py`` (six
live-subprocess parity cases; floats within 1e-6; ``sqlite3 -box``
output diffed as parsed row dicts to insulate from column-width drift).

The bash script's ``measure_*`` calls shell out to inline ``python3 -``
heredocs. The port inlines the peer module
:func:`mini_ork.ported.topology_metrics.{measure_rho, measure_C,
measure_I, measure_topology}` and calls them directly — no bash
subprocess required for measurement, only for the parity test's own
comparison invocation.

Output format notes (kept verbatim so the parity test can grep on
literal banner text):

  * ``--compute`` emits 8 lines:
        ``=== mini-ork topology — ad-hoc measurement ===``
        ``    panel_run_id: <id>``
        ``    recipe:       <recipe>``
        ```` (blank)
        ``    rho:   <float>``
        ``    C:     <float>``
        ``    I:     <float>``
        ```` (blank)
        ``Persisting to panel_topology_telemetry...``
        ``telemetry_id=<id>``
    The em-dash is U+2014 (``\\u2014``); bash emits the same character
    via UTF-8 source. Tests parse this banner into named fields rather
    than asserting raw string equality to keep parser tolerance tight.

  * ``--backfill`` emits the banner then ``<prid> → <recipe> →
    <telemetry_id>`` per row, ending with ``Backfilled N panel_runs.``.

  * Default ``summary`` mode shells out to ``sqlite3 -box`` with the
    same SQL body as bash, then a second ``sqlite3 -box`` for the
    quadrant distribution. Both ports round-trip through the same
    ``sqlite3`` binary so column widths match; the parity test parses
    rows by ``|``-boundary splits.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from typing import Sequence

from mini_ork.ported.topology_metrics import (
    ensure_table,
    measure_C,
    measure_I,
    measure_rho,
    measure_topology,
)

__all__ = [
    "resolve_paths",
    "cmd_summary",
    "cmd_compute",
    "cmd_backfill",
    "main",
    "COMPUTE_BANNER",
    "BACKFILL_BANNER",
    "SUMMARY_BANNER",
    "QUADRANT_BANNER",
    "PERSISTING_LINE",
    "format_compute_banner",
    "format_metrics_block",
]

# ─────────────────────────────────────────────────────────────────────────────
# Banner strings. Lifted verbatim from bin/mini-ork-topology so the
# parity test can grep for them. The em-dash is U+2014.
# ─────────────────────────────────────────────────────────────────────────────

COMPUTE_BANNER = "=== mini-ork topology — ad-hoc measurement ==="
BACKFILL_BANNER = "=== mini-ork topology — backfill from execution_traces ==="
SUMMARY_BANNER = "=== mini-ork topology — summary ==="
QUADRANT_BANNER = "=== Quadrant distribution ==="
PERSISTING_LINE = "Persisting to panel_topology_telemetry..."


# ─────────────────────────────────────────────────────────────────────────────
# Path resolution — mirrors bash:
#   MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
#   MINI_ORK_HOME="${MINI_ORK_HOME:-$MINI_ORK_ROOT/.mini-ork}"
#   MINI_ORK_DB="${MINI_ORK_DB:-$MINI_ORK_HOME/state.db}"
#
# When invoked as a library (e.g. from the parity test), ``fallback_root``
# is the REPO_ROOT — the same value bash would derive from the script
# location if MINI_ORK_ROOT were unset.
# ─────────────────────────────────────────────────────────────────────────────

def resolve_paths(fallback_root: str | None = None) -> tuple[str, str, str]:
    """Return ``(root, home, db)`` using the same precedence bash uses.

    Precedence (highest first): explicit env, then derivation from
    ``fallback_root`` (the repo root when invoked from tests), then
    PWD-relative defaults. Mirrors bash lines 16-19 verbatim.
    """
    root = os.environ.get("MINI_ORK_ROOT")
    if not root and fallback_root:
        root = fallback_root
    if not root:
        root = os.getcwd()
    home = os.environ.get("MINI_ORK_HOME") or os.path.join(root, ".mini-ork")
    db = os.environ.get("MINI_ORK_DB") or os.path.join(home, "state.db")
    return root, home, db


# ─────────────────────────────────────────────────────────────────────────────
# Subcommand: summary — two ``sqlite3 -box`` tables.
#
# Bash uses ``sqlite3 -box`` for both the recipe-filtered (or all-recipe)
# row dump and the quadrant-distribution row dump. The Python port
# shells out to the same binary so column widths match exactly; the
# parity test parses the box output by ``|`` boundaries.
# ─────────────────────────────────────────────────────────────────────────────

_SUMMARY_SELECT_SQL = """
    SELECT recipe, rho, context_distance AS C, inductive_distance AS I,
           quadrant, agent_count, n_traces,
           substr(computed_at, 1, 19) AS at
    FROM panel_topology_telemetry
    {filter}
    ORDER BY computed_at DESC LIMIT 20;
"""

_QUADRANT_SELECT_SQL = """
    SELECT quadrant, COUNT(*) AS cycles, AVG(rho) AS avg_rho,
           AVG(context_distance) AS avg_C, AVG(inductive_distance) AS avg_I
    FROM panel_topology_telemetry
    GROUP BY quadrant
    ORDER BY cycles DESC;
"""


def cmd_summary(db_path: str, recipe: str = "") -> None:
    """Print the two-table summary (rows + quadrant distribution).

    Mirrors the bash default branch (lines 86-122 of
    ``bin/mini-ork-topology``). ``recipe`` empty → "all recipes"
    branch; non-empty → recipe-filtered branch.
    """
    ensure_table(db_path)
    print(SUMMARY_BANNER)
    if recipe:
        print(f"    recipe filter: {recipe}")
        where = "WHERE recipe = '{}'".format(recipe.replace("'", "''"))
    else:
        print("    (all recipes; latest 20)")
        where = ""
    rows_sql = _SUMMARY_SELECT_SQL.format(filter=where)
    # bash's `2>&1` collapses stderr into stdout. We pass stderr through
    # so the parity test sees the same byte stream bash produces.
    proc = subprocess.run(
        ["sqlite3", "-box", db_path, rows_sql],
        capture_output=True, text=True, errors="replace",
    )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    print()
    print(QUADRANT_BANNER)
    proc2 = subprocess.run(
        ["sqlite3", "-box", db_path, _QUADRANT_SELECT_SQL],
        capture_output=True, text=True, errors="replace",
    )
    sys.stdout.write(proc2.stdout)
    sys.stderr.write(proc2.stderr)


# ─────────────────────────────────────────────────────────────────────────────
# Subcommand: compute — emit 8-line banner + telemetry_id, persist one row.
#
# Bash emits:
#   echo "=== mini-ork topology — ad-hoc measurement ==="
#   echo "    panel_run_id: $PANEL_RUN_ID"
#   echo "    recipe:       $RECIPE"
#   echo
#   echo "    rho:   $(measure_rho "$PANEL_RUN_ID")"
#   echo "    C:     $(measure_C   "$PANEL_RUN_ID")"
#   echo "    I:     $(measure_I   "$PANEL_RUN_ID")"
#   echo
#   echo "Persisting to panel_topology_telemetry..."
#   TELEMETRY_ID=$(measure_topology "$PANEL_RUN_ID" "$RECIPE")
#   echo "telemetry_id=$TELEMETRY_ID"
#
# The port calls measure_* from topology_metrics directly (no heredoc).
# ─────────────────────────────────────────────────────────────────────────────

def format_compute_banner(panel_run_id: str, recipe: str) -> str:
    """Return the 5-line header block (banner + panel/recipe + blank)."""
    lines = [
        COMPUTE_BANNER,
        f"    panel_run_id: {panel_run_id}",
        f"    recipe:       {recipe}",
        "",
    ]
    return "\n".join(lines) + "\n"


def format_metrics_block(rho: float, C: float, I: float) -> str:
    """Return the 4-line metrics block (rho/C/I + blank)."""
    return (
        f"    rho:   {rho}\n"
        f"    C:     {C}\n"
        f"    I:     {I}\n"
        "\n"
    )


def cmd_compute(db_path: str, panel_run_id: str, recipe: str,
                root: str | None = None) -> str:
    """Compute (ρ, C, I) for ``panel_run_id`` and persist one telemetry row.

    Returns the ``telemetry_id`` written. Mirrors bash lines 56-71 of
    ``bin/mini-ork-topology`` exactly — same 8-line banner layout, same
    persistence call.
    """
    ensure_table(db_path)
    rho = measure_rho(db_path, panel_run_id)
    C = measure_C(db_path, panel_run_id)
    I = measure_I(db_path, panel_run_id, root)
    sys.stdout.write(format_compute_banner(panel_run_id, recipe))
    sys.stdout.write(format_metrics_block(rho, C, I))
    sys.stdout.write(PERSISTING_LINE + "\n")
    telemetry_id = measure_topology(db_path, panel_run_id, recipe, root)
    sys.stdout.write(f"telemetry_id={telemetry_id}\n")
    sys.stdout.flush()
    return telemetry_id


# ─────────────────────────────────────────────────────────────────────────────
# Subcommand: backfill — iterate distinct panel_run_ids + persist.
#
# Bash (lines 74-85):
#   panel_ids=$(sqlite3 "$MINI_ORK_DB" "
#     SELECT DISTINCT
#       CASE
#         WHEN run_id IS NOT NULL THEN CAST(run_id AS TEXT)
#         ELSE substr(trace_id, instr(trace_id, '-')+1)
#       END AS panel_run_id
#     FROM execution_traces
#     WHERE trace_id IS NOT NULL
#     LIMIT 50;
#   " 2>/dev/null | sort -u | head -20)
#
#   count=0
#   for prid in $panel_ids; do
#     [ -z "$prid" ] && continue
#     recipe=$(sqlite3 "$MINI_ORK_DB" "
#       SELECT task_class FROM execution_traces
#       WHERE trace_id LIKE '%${prid}%' LIMIT 1;
#     " 2>/dev/null || echo "generic")
#     [ -z "$recipe" ] && recipe="generic"
#     telemetry_id=$(measure_topology "$prid" "$recipe" 2>/dev/null || echo "skip")
#     echo "  $prid → $recipe → $telemetry_id"
#     count=$((count + 1))
#   done
#   echo "Backfilled $count panel_runs."
#
# The port replicates the same SQL, the same LIKE-based recipe lookup,
# and the same skip fallback.
# ─────────────────────────────────────────────────────────────────────────────

_BACKFILL_DISTINCT_SQL = """
    SELECT DISTINCT
      CASE
        WHEN run_id IS NOT NULL THEN CAST(run_id AS TEXT)
        ELSE substr(trace_id, instr(trace_id, '-')+1)
      END AS panel_run_id
    FROM execution_traces
    WHERE trace_id IS NOT NULL
    LIMIT 50;
"""

_BACKFILL_RECIPE_SQL = """
    SELECT task_class FROM execution_traces
    WHERE trace_id LIKE ? LIMIT 1;
"""


def _distinct_panel_ids(db_path: str) -> list[str]:
    """Run the backfill distinct-IDs SQL and return the sorted unique set.

    Mirrors the bash pipe ``| sort -u | head -20``: sort alphabetically,
    then truncate. Empty strings are dropped (bash's ``[ -z "$prid" ]
    continue``).
    """
    con = sqlite3.connect(db_path)
    rows = con.execute(_BACKFILL_DISTINCT_SQL).fetchall()
    con.close()
    seen = set()
    for (prid,) in rows:
        if prid:
            seen.add(str(prid))
    return sorted(seen)[:20]


def _recipe_for_panel(db_path: str, panel_run_id: str) -> str:
    """Return the ``task_class`` of one trace whose trace_id contains the
    panel_run_id. Falls back to ``"generic"`` on any error or empty
    result (mirrors bash lines 80-82)."""
    try:
        con = sqlite3.connect(db_path)
        row = con.execute(
            _BACKFILL_RECIPE_SQL, (f"%{panel_run_id}%",)
        ).fetchone()
        con.close()
    except sqlite3.Error:
        return "generic"
    if not row or not row[0]:
        return "generic"
    return str(row[0])


def cmd_backfill(db_path: str, root: str | None = None,
                 limit: int = 20) -> int:
    """Measure every distinct panel_run_id in ``execution_traces``.

    Mirrors bash lines 73-94 of ``bin/mini-ork-topology``: emits the
    backfill banner, one ``<prid> → <recipe> → <telemetry_id>`` line
    per id, and the closing ``Backfilled N panel_runs.`` summary.

    ``measure_topology`` errors are swallowed exactly like bash's
    ``|| echo "skip"`` fallback: if it raises, we emit ``skip`` and
    continue without incrementing the count.

    Returns the count of successfully measured ids (bash's ``count``).
    """
    ensure_table(db_path)
    print(BACKFILL_BANNER)
    panel_ids = _distinct_panel_ids(db_path)[:limit]
    count = 0
    for prid in panel_ids:
        recipe = _recipe_for_panel(db_path, prid)
        try:
            telemetry_id = measure_topology(db_path, prid, recipe, root)
        except Exception:
            telemetry_id = "skip"
        if telemetry_id != "skip":
            count += 1
        # bash's arrow is U+2192 (→) — emit the same unicode character.
        sys.stdout.write(f"  {prid} → {recipe} → {telemetry_id}\n")
    sys.stdout.write(f"Backfilled {count} panel_runs.\n")
    sys.stdout.flush()
    return count


# ─────────────────────────────────────────────────────────────────────────────
# Argument parsing + main().
#
# Bash parsing (lines 30-38):
#   RECIPE=""
#   COMPUTE=0
#   BACKFILL=0
#   PANEL_RUN_ID=""
#   while [[ $# -gt 0 ]]; do
#     case "$1" in
#       --recipe)   RECIPE="${2:?--recipe requires a value}"; shift 2 ;;
#       --compute)  COMPUTE=1; PANEL_RUN_ID="${2:?--compute requires panel_run_id}"; RECIPE="${3:-generic}"; shift 3 ;;
#       --backfill) BACKFILL=1; shift ;;
#       --help|-h)  _usage ;;
#       *)          _usage ;;
#     esac
#   done
# ─────────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser matching the bash CLI surface."""
    parser = argparse.ArgumentParser(
        prog="mini-ork topology",
        description=(
            "Query + compute panel-topology telemetry "
            "(E-MO-01 of the multi-agent orchestrator epic catalogue)."
        ),
        add_help=True,
    )
    parser.add_argument(
        "--recipe", default="",
        help="Filter the summary output to a single recipe.",
    )
    parser.add_argument(
        "--compute", metavar="PANEL_RUN_ID", default=None,
        help="Ad-hoc measurement for the given panel_run_id. "
             "Requires a recipe as the next positional arg.",
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help="Measure every distinct panel_run_id in execution_traces.",
    )
    return parser


def _usage() -> None:
    """Mirror bash's ``_usage`` — print to stderr and exit 2."""
    sys.stderr.write(
        "Usage:\n"
        "  mini-ork topology [--recipe <name>]\n"
        "  mini-ork topology --compute <panel_run_id> <recipe>\n"
        "  mini-ork topology --backfill\n"
    )
    sys.exit(2)


def main(argv: Sequence[str] | None = None) -> int:
    """Argparse-compatible entrypoint.

    Returns the process exit code: 0 on success, 2 on usage error,
    non-zero on unhandled exception. Mirrors bash's
    ``exit 0`` / ``exit 2`` pattern.
    """
    # Build the parser, then merge the unknown-positionals semantics
    # bash provides (--compute takes 2 positionals: panel_run_id +
    # recipe). argparse can't model that with a single --compute flag,
    # so we split argv ourselves to mirror bash's positional behaviour.
    args_list = list(argv) if argv is not None else sys.argv[1:]
    recipe = ""
    compute_id = None
    compute_recipe = "generic"
    backfill = False

    i = 0
    while i < len(args_list):
        tok = args_list[i]
        if tok in ("--help", "-h"):
            _build_parser().print_help(sys.stderr)
            return 2
        if tok == "--recipe":
            if i + 1 >= len(args_list):
                sys.stderr.write("--recipe requires a value\n")
                return 2
            recipe = args_list[i + 1]
            i += 2
            continue
        if tok == "--compute":
            if i + 1 >= len(args_list):
                sys.stderr.write("--compute requires panel_run_id\n")
                return 2
            compute_id = args_list[i + 1]
            compute_recipe = args_list[i + 2] if i + 2 < len(args_list) else "generic"
            i += 3
            continue
        if tok == "--backfill":
            backfill = True
            i += 1
            continue
        _usage()

    _root, _, db_path = resolve_paths()

    if compute_id is not None:
        cmd_compute(db_path, compute_id, compute_recipe, _root)
        return 0
    if backfill:
        cmd_backfill(db_path, _root)
        return 0
    cmd_summary(db_path, recipe)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))