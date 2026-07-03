"""Pure-logic port of ``lib/rho_aggregator.sh``.

Faithful port of the two pure/deterministic callables of the bash
Retrospective Harness Optimization (RHO) aggregator (arXiv:2606.05922).
The bash file resolves ``STATE_DB`` at *source* time from
``${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db}``; the Python
port takes the database path as a parameter instead, which is the
controllable equivalent.

Bash function inventory (verbatim from ``lib/rho_aggregator.sh``)::

    rho_aggregate_win_rates [--since EPOCH] [--task-class X]
        Re-aggregate from ``execution_traces``. Idempotent: upserts the
        row keyed by ``(prompt_version_hash, task_class)`` into
        ``prompt_win_rates``. Emits the row count on stdout.

    rho_top_prompts <task_class> <node_type> [<top_n>]
        Print the top-N prompts by ``win_rate`` for the given
        ``task_class`` + ``node_type`` filter, ``sample_size >= 3`` to
        avoid one-shot noise.

Parity-mapping contract (must remain exact)::

    bash                                     python
    -------------------------------------------------------------------------------
    rho_aggregate_win_rates [..]   ─►  aggregate_win_rates(state_db, since=0,
                                                            task_class="")
                                       returns int (row count printed by bash)
    rho_top_prompts tc nt [n]      ─►  top_prompts(state_db, task_class, node_type="",
                                                    top_n=5)
                                       returns str (the full multi-line stdout,
                                       trailing newline preserved)

Output-format parity notes (verified against live bash on a 3-trace seed)::

    * ``win_rate`` is rounded to 4 dp on insert (``round(wr, 4)``) and
      printed at 3 dp by ``top_prompts`` via ``printf '%.3f'``. Python
      mirrors with ``round(x, 4)`` / ``f"{x:.3f}"``.
    * ``sample_size`` printed as ``printf '%4d'`` (width 4, right-aligned).
    * Hash printed as ``printf '%-12s'`` over the first 12 chars (left-aligned).
    * ``node_type`` printed via ``COALESCE(node_type,'?')`` — so a NULL
      node_type always renders as ``?``.
    * Filter clause ``(node_type IS NULL OR node_type = X)`` means a
      NULL node_type row passes any node_type filter; this is preserved.

Float equality is enforced at ``<1e-6`` for ``win_rate``-shaped outputs;
string equality is exact after ``.strip()`` for the formatted table.

``tests/unit/test_rho_aggregator_parity.py`` enforces this parity over a
fixture corpus of ``>=6`` cases by shelling out to live bash via
``subprocess.run`` and comparing byte-for-byte.
"""

from __future__ import annotations

import sqlite3


def _connect(state_db: str) -> sqlite3.Connection:
    """Open ``state_db`` with the same pragma the bash heredoc uses."""
    con = sqlite3.connect(state_db)
    con.execute("PRAGMA busy_timeout=5000")
    return con


def aggregate_win_rates(state_db: str, since: int = 0, task_class: str = "") -> int:
    """Python equivalent of ``rho_aggregate_win_rates [--since E] [--task-class X]``.

    Walks ``execution_traces``, groups by ``(prompt_version_hash, task_class)``,
    upserts into ``prompt_win_rates``, and returns the row count (the same
    integer bash prints on stdout).

    Win/loss/tie rubric mirrors the bash heredoc verbatim:

      * win  : ``status='success'`` AND ``reviewer_verdict`` NOT IN
               (``REJECT``, ``ESCALATE``, ``needs_revision``) AND not NULL
      * loss : ``status='failure'`` OR
               (``status='success'`` AND ``reviewer_verdict`` IN the above)
      * tie  : ``status`` IN (``running``, ``vacuous``, ``blocked``, ``unknown``)
               OR NULL

    ``win_rate = wins / (wins + losses)`` if denominator > 0 else ``0.0``;
    rounded to 4 decimal places before insert (matches bash's
    ``round(wr, 4)``).
    """
    import datetime as _dt

    since_iso = _dt.datetime.utcfromtimestamp(int(since)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    con = _connect(state_db)

    clauses = [
        "created_at >= ?",
        "prompt_version_hash IS NOT NULL",
        "prompt_version_hash <> ''",
        "task_class IS NOT NULL",
        "task_class <> ''",
    ]
    params: list = [since_iso]
    if task_class:
        clauses.append("task_class = ?")
        params.append(task_class)

    sql = f"""
        SELECT prompt_version_hash, task_class,
            SUM(CASE
                  WHEN status='success'
                   AND (reviewer_verdict IS NULL
                        OR reviewer_verdict NOT IN ('REJECT','ESCALATE','needs_revision'))
                  THEN 1 ELSE 0 END) AS wins,
            SUM(CASE
                  WHEN status='failure'
                    OR (status='success' AND reviewer_verdict IN ('REJECT','ESCALATE','needs_revision'))
                  THEN 1 ELSE 0 END) AS losses,
            SUM(CASE
                  WHEN status IN ('running','vacuous','blocked','unknown')
                       OR status IS NULL
                  THEN 1 ELSE 0 END) AS ties
          FROM execution_traces
         WHERE {' AND '.join(clauses)}
         GROUP BY prompt_version_hash, task_class
    """
    rows = con.execute(sql, params).fetchall()

    updated = 0
    for prompt, tc, wins, losses, ties in rows:
        n = (wins or 0) + (losses or 0) + (ties or 0)
        wr = (wins / (wins + losses)) if (wins + losses) > 0 else 0.0
        con.execute(
            """
            INSERT INTO prompt_win_rates
                (prompt_version_hash, task_class, wins, losses, ties,
                 win_rate, sample_size,
                 last_updated)
            VALUES (?,?,?,?,?,?,?,
                    strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            ON CONFLICT(prompt_version_hash, task_class) DO UPDATE SET
                wins=excluded.wins, losses=excluded.losses, ties=excluded.ties,
                win_rate=excluded.win_rate, sample_size=excluded.sample_size,
                last_updated=excluded.last_updated
            """,
            (prompt, tc, wins or 0, losses or 0, ties or 0, round(wr, 4), n),
        )
        updated += 1

    con.commit()
    con.close()
    return updated


def top_prompts(state_db: str, task_class: str, node_type: str = "", top_n: int = 5) -> str:
    """Python equivalent of ``rho_top_prompts <task_class> <node_type> [<top_n>]``.

    Returns the full multi-line formatted table bash would print, with the
    trailing newline preserved (bash's ``sqlite3 ... ;`` emits one).

    Columns: ``win_rate`` (3 dp), ``sample_size`` (width-4 right-aligned),
    ``prompt_version_hash`` (first 12 chars, width-12 left-aligned),
    ``node_type`` (``?`` if NULL).
    """
    where = f"task_class='{task_class}' AND sample_size >= 3"
    if node_type:
        where += f" AND (node_type IS NULL OR node_type='{node_type}')"

    con = _connect(state_db)
    rows = con.execute(
        f"""
        SELECT printf('%.3f', win_rate),
                printf('%4d', sample_size),
                printf('%-12s', substr(prompt_version_hash,1,12)),
                COALESCE(node_type,'?')
           FROM prompt_win_rates
          WHERE {where}
          ORDER BY win_rate DESC, sample_size DESC
          LIMIT {int(top_n)};
        """
    ).fetchall()
    con.close()

    return "\n".join(" | ".join(str(c) for c in row) for row in rows) + ("\n" if rows else "")