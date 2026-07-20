"""Role-evolver — Python port of lib/role_evolver.sh.

Faithful port of the four bash functions that compose role-evolution proposals
from observed signals and read/update the ``role_evolver_log`` table. The bash
wrapper in ``lib/role_evolver.sh`` is the authoritative source; this module
mirrors its SQL semantics, output, and idempotence so parity tests can
diff against the live bash invocation byte-for-byte (floats 1e-6).

Co-existence model (strangler-fig): bash ``lib/role_evolver.sh`` stays in
place. The Python port gives callers an in-process surface and gives the
parity test (``tests/unit/test_role_evolver_py.py``) a stable target to
diff against the live bash.

Pipeline map (bash function → Python):
  role_evolver_propose:
    Signal 1 retire query (agent_performance_memory)            → _loser_lanes
    Signal 2 split query  (bug_reports + correlated sub-select)  → _bug_clusters
    Signal 3 rename query (gradient_records cross_class)         → _cross_class_renames
    Idempotent INSERT (target_recipe, target_node_id, kind)     → propose
  role_evolver_list     (printf-formatted pipe-separated rows)  → list_proposals
  role_evolver_accept                                             → accept
  role_evolver_reject                                             → reject
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

__all__ = ["propose", "list_proposals", "accept", "reject"]


def _resolve_db(db: str | os.PathLike | None) -> str:
    """Mirror bash line 32 precedence: MINI_ORK_DB > ${MINI_ORK_HOME:-.mini-ork}/state.db.

    When the caller passes an explicit ``db`` (the test fixture and most
    programmatic callers do), that wins. Otherwise the bash precedence
    applies so an in-process port behaves identically when invoked with
    the same env the bash wrapper would see.
    """
    if db is not None and os.fspath(db):
        return os.fspath(db)
    explicit = os.environ.get("MINI_ORK_DB")
    if explicit:
        return explicit
    home = os.environ.get("MINI_ORK_HOME") or ".mini-ork"
    return os.path.join(home, "state.db")


def _open(db: str) -> sqlite3.Connection:
    """Open the SQLite DB with the busy_timeout the bash heredoc sets.

    Mirrors ``PRAGMA busy_timeout=5000`` from lib/role_evolver.sh so the port
    doesn't deadlock against concurrent writers (e.g. the verifier running
    init.sh against the same DB while propose() is mid-insert).
    """
    con = sqlite3.connect(db)
    con.execute("PRAGMA busy_timeout=5000")
    con.row_factory = sqlite3.Row
    return con


def _loser_lanes(db: str, class_filter: str, top: int) -> list[sqlite3.Row]:
    """Signal 1: lanes with strongly negative relative_advantage.

    Mirrors lib/role_evolver.sh lines 56-65:

        WHERE runs_count >= 3 AND relative_advantage <= -0.20
              [AND task_class=?]
        ORDER BY relative_advantage ASC LIMIT ?
    """
    clauses = "runs_count >= 3 AND relative_advantage <= -0.20"
    params: tuple = (top,)
    if class_filter:
        clauses += " AND task_class=?"
        params = (class_filter, top)
    con = _open(db)
    try:
        return con.execute(
            f"""
            SELECT agent_version_id AS lane, task_class, role, model,
                   relative_advantage, runs_count
              FROM agent_performance_memory
             WHERE {clauses}
             ORDER BY relative_advantage ASC LIMIT ?
            """,
            params,
        ).fetchall()
    finally:
        con.close()


def _bug_clusters(db: str, top: int) -> list[sqlite3.Row]:
    """Signal 2: high-frequency bug_reports clustered by agent_role.

    Mirrors lib/role_evolver.sh lines 81-93. The correlated sub-select for
    ``top_title`` orders by severity='critical' DESC, frequency DESC — the
    port must keep this exact ordering or the rationale's top_title shifts.
    """
    con = _open(db)
    try:
        return con.execute(
            """
            SELECT agent_role, COUNT(*) AS n, MAX(frequency) AS max_freq,
                   GROUP_CONCAT(id) AS bug_ids,
                   (SELECT title FROM bug_reports b2
                      WHERE b2.agent_role = b.agent_role AND b2.status='open'
                      ORDER BY severity='critical' DESC, frequency DESC LIMIT 1
                   ) AS top_title
              FROM bug_reports b
             WHERE status='open' AND severity IN ('high','critical')
             GROUP BY agent_role
            HAVING n >= 2
             ORDER BY n DESC LIMIT ?
            """,
            (top,),
        ).fetchall()
    finally:
        con.close()


def _cross_class_renames(db: str, top: int) -> list[sqlite3.Row]:
    """Signal 3: workflow nodes appearing in __cross_class__ gradients.

    Mirrors lib/role_evolver.sh lines 112-120. ``node_name`` is extracted
    via bash's ``split('.')[-1]`` semantics — the LAST segment of the target
    string, which matches Python's ``rsplit('.', 1)[-1]`` and ``split('.')[-1]``
    for non-empty inputs.
    """
    con = _open(db)
    try:
        return con.execute(
            """
            SELECT target, MAX(confidence) AS top_conf, COUNT(*) AS n
              FROM gradient_records
             WHERE task_class='__cross_class__'
               AND target LIKE 'cross_class:workflow.node.%'
               AND confidence >= 0.85
             GROUP BY target
             ORDER BY top_conf DESC, n DESC LIMIT ?
            """,
            (top,),
        ).fetchall()
    finally:
        con.close()


def propose(
    db: str | os.PathLike | None = None,
    class_filter: str = "",
    top: int = 5,
) -> int:
    """Mirror lib/role_evolver.sh::role_evolver_propose.

    Composes the three signals into role_evolver_log rows. Idempotent on
    (target_recipe, target_node_id, proposal_kind) — duplicate 'open' rows
    are skipped. Returns the count of newly-inserted proposals (an int, so
    the bash wrapper's ``print(inserted)`` contract holds).
    """
    dbp = _resolve_db(db)

    losers = _loser_lanes(dbp, class_filter, top)
    bug_clusters = _bug_clusters(dbp, top)
    cross_targets = _cross_class_renames(dbp, top)

    proposals: list[dict] = []

    for r in losers:
        proposals.append({
            "target_recipe":   r["task_class"].replace("_", "-"),
            "target_node_id":  r["lane"],
            "proposal_kind":   "retire",
            "rationale":       (f"Lane {r['lane']} has relative_advantage "
                                f"{r['relative_advantage']:.2f} over "
                                f"{r['runs_count']} runs in {r['task_class']} — "
                                f"consistently underperforms its peers."),
            "evidence_json":   json.dumps({"agent_perf_rows": [r["lane"]]}),
            "proposed_change": f"# Remove {r['lane']} from {r['task_class']} lens panel",
        })

    for r in bug_clusters:
        if not r["agent_role"]:
            continue
        proposals.append({
            "target_recipe":   "framework-edit",
            "target_node_id":  r["agent_role"],
            "proposal_kind":   "split",
            "rationale":       (f"Role {r['agent_role']} accumulated {r['n']} "
                                f"high/critical bug_reports (top title: "
                                f"{(r['top_title'] or '')[:80]!r}). Splitting "
                                f"into focused sub-roles may reduce surface."),
            "evidence_json":   json.dumps({"bug_ids": (r["bug_ids"] or "").split(",")[:10]}),
            "proposed_change": f"# Split {r['agent_role']} into {r['agent_role']}_pre and {r['agent_role']}_post",
        })

    for r in cross_targets:
        target = r["target"]
        node_name = target.split(".")[-1] if target else "?"
        if not node_name or node_name == "?":
            continue
        proposals.append({
            "target_recipe":   "*",
            "target_node_id":  node_name,
            "proposal_kind":   "rename",
            "rationale":       (f"Node '{node_name}' surfaces as a "
                                f"__cross_class__ gradient with confidence "
                                f"{r['top_conf']:.2f}. Lessons generalize — "
                                f"consider naming abstractly."),
            "evidence_json":   json.dumps({"gradient_target": target}),
            "proposed_change": f"# Rename node {node_name} -> <abstract noun>",
        })

    con = _open(dbp)
    try:
        inserted = 0
        now = int(time.time())
        for p in proposals:
            exists = con.execute(
                """
                SELECT 1 FROM role_evolver_log
                 WHERE target_recipe=? AND target_node_id=? AND proposal_kind=?
                   AND status='open' LIMIT 1
                """,
                (p["target_recipe"], p["target_node_id"], p["proposal_kind"]),
            ).fetchone()
            if exists:
                continue
            con.execute(
                """
                INSERT INTO role_evolver_log
                    (proposed_at, target_recipe, target_node_id, proposal_kind,
                     rationale, evidence_json, proposed_change, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'open')
                """,
                (now, p["target_recipe"], p["target_node_id"], p["proposal_kind"],
                 p["rationale"][:600], p["evidence_json"][:2000],
                 p["proposed_change"][:600]),
            )
            inserted += 1
        con.commit()
    finally:
        con.close()
    return inserted


def list_proposals(
    db: str | os.PathLike | None = None,
    status: str = "open",
    limit: int = 20,
) -> str:
    """Mirror lib/role_evolver.sh::role_evolver_list.

    The bash printf format emits pipe-separated rows of width-padded columns.
    The Python port must reproduce the same widths so the parity test's
    string-equality check holds:

      printf('%-4d', id)                                → str(id)[:4].ljust(4)
      printf('%-10s', status)                           → status.ljust(10)
      printf('%-7s', proposal_kind)                     → proposal_kind.ljust(7)
      printf('%-18s', substr(target_recipe, 1, 18))     → target_recipe[:18].ljust(18)
      printf('%-15s', substr(target_node_id, 1, 15))    → target_node_id[:15].ljust(15)
      substr(rationale, 1, 80)                          → rationale[:80]

    Status handling mirrors bash: ``--`` prefix is stripped, ``status='all'``
    or empty status yields no WHERE clause, anything else is bound as
    ``WHERE status='<value>'``. The LIMIT mirrors bash's ``LIMIT 20`` (the
    ``limit`` parameter lets the parity test exercise a smaller window).
    """
    dbp = _resolve_db(db)
    # Mirror bash: ${1:---open} then ${status#--}. A leading '--' is stripped.
    if status.startswith("--"):
        status = status[2:]
    clause = ""
    params: tuple = ()
    if status and status != "all":
        clause = "WHERE status=?"
        params = (status,)

    con = _open(dbp)
    try:
        rows = con.execute(
            f"""
            SELECT id, status, proposal_kind, target_recipe, target_node_id,
                   rationale
              FROM role_evolver_log
              {clause}
             ORDER BY id DESC LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
    finally:
        con.close()

    sep = " | "
    out_lines = []
    for row in rows:
        out_lines.append(
            str(row["id"])[:4].ljust(4) + sep
            + (row["status"] or "").ljust(10) + sep
            + (row["proposal_kind"] or "").ljust(7) + sep
            + (row["target_recipe"] or "")[:18].ljust(18) + sep
            + (row["target_node_id"] or "")[:15].ljust(15) + sep
            + (row["rationale"] or "")[:80]
        )
    return "\n".join(out_lines)


def accept(db: str | os.PathLike | None, proposal_id: int) -> None:
    """Mirror lib/role_evolver.sh::role_evolver_accept.

    Raises ValueError on falsy id (mirrors bash's ``${1:?id required}``).
    No silent fallbacks: forbidden_fallbacks bans swallowing the missing-id
    case, so we surface it instead of silently no-oping.
    """
    if not proposal_id:
        raise ValueError("id required")
    dbp = _resolve_db(db)
    con = _open(dbp)
    try:
        con.execute(
            "UPDATE role_evolver_log SET status='accepted' WHERE id=?;",
            (proposal_id,),
        )
        con.commit()
    finally:
        con.close()


def reject(db: str | os.PathLike | None, proposal_id: int) -> None:
    """Mirror lib/role_evolver.sh::role_evolver_reject.

    Raises ValueError on falsy id (mirrors bash's ``${1:?id required}``).
    """
    if not proposal_id:
        raise ValueError("id required")
    dbp = _resolve_db(db)
    con = _open(dbp)
    try:
        con.execute(
            "UPDATE role_evolver_log SET status='rejected' WHERE id=?;",
            (proposal_id,),
        )
        con.commit()
    finally:
        con.close()