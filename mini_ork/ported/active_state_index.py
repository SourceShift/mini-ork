"""active_state_index.py — Python port of lib/active_state_index.sh.

Faithful strangler-fig port (HarnessBridge Technique 1, arxiv:2606.12882).
The bash function ``mo_active_state_block`` in ``lib/active_state_index.sh``
stays in place; this module gives Python callers an in-process target and
gives tests a stable surface for parity verification against the live
bash subprocess.

Public API:
    render_active_state_block(task_class='__any__', days_window=30,
                              max_per_section=None, db_path=None,
                              disabled=None) -> str

Behavior mirrors the bash function byte-for-byte:
    - MO_DISABLE_ACTIVE_STATE=1 (env) or disabled=True → return ""
    - Internal helpers (_unresolved_errors, _open_constraints,
      _established_facts, _pending_goals) guard on table presence and
      return [] when the relevant sqlite_master row is missing.
    - DECISION_VARIABLES is the static 6-entry list; the empty-block
      shortcut (total==0 AND len(d)==0) is preserved verbatim even
      though it's unreachable with the static list.
    - Final assembly prints the markdown wrapper ('--- ACTIVE STATE
      INDEX ... ---' fences + ```json ...``` + optional
      '**Summary:** N ...' line) byte-equal to bash output.

Env knobs (read at call time, matching bash's lookup order):
    MINI_ORK_DB                    — db path (overridable via db_path kwarg)
    MO_DISABLE_ACTIVE_STATE=1      — short-circuit to empty string
    MO_ACTIVE_STATE_MAX_PER_SECTION — cap per section (default 5)
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

# Static 6-entry list — verbatim mirror of the bash heredoc at
# lib/active_state_index.sh:194-203 (DECISION_VARIABLES).
DECISION_VARIABLES: list[dict[str, str]] = [
    {"knob": "MO_DAILY_BUDGET_USD", "kind": "cost-cap", "scope": "global"},
    {"knob": "MO_TIER4_QUORUM", "kind": "panel-quorum", "scope": "per-recipe"},
    {"knob": "MO_DISABLE_CN", "kind": "context-source", "scope": "per-run"},
    {"knob": "MO_INJECT_LEARNINGS", "kind": "context-injection", "scope": "per-run"},
    {"knob": "MO_DISABLE_ACTIVE_STATE", "kind": "context-injection", "scope": "per-run"},
    {"knob": "MO_REFUSE_UNSANDBOXED", "kind": "safety-threshold", "scope": "per-recipe"},
]


def _default_db_path() -> str:
    """Mirror _mo_asi_db() in bash: MINI_ORK_DB else ${MINI_ORK_HOME:-$(pwd)/.mini-ork}/state.db."""
    db = os.environ.get("MINI_ORK_DB")
    if db:
        return db
    home = os.environ.get("MINI_ORK_HOME") or os.getcwd()
    return f"{home}/.mini-ork/state.db"


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    cur = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    )
    return cur.fetchone() is not None


def _unresolved_errors(con: sqlite3.Connection, max_n: int, days: int) -> list[dict[str, Any]]:
    """Mirror _mo_asi_unresolved_errors (lib/active_state_index.sh:56)."""
    if not _table_exists(con, "failure_memory"):
        return []
    cur = con.execute(
        """SELECT failure_id, workflow_stage, failure_category, error_message, occurred_at
           FROM failure_memory
           WHERE occurred_at >= datetime('now', ?)
           ORDER BY occurred_at DESC LIMIT ?""",
        (f"-{days} days", max_n),
    )
    out: list[dict[str, Any]] = []
    for r in cur:
        out.append({
            "failure_id": r["failure_id"],
            "workflow_stage": r["workflow_stage"],
            "failure_category": r["failure_category"],
            "error_message": (r["error_message"] or "")[:200],
            "occurred_at": r["occurred_at"],
        })
    return out


def _open_constraints(con: sqlite3.Connection, max_n: int, days: int) -> list[dict[str, Any]]:
    """Mirror _mo_asi_open_constraints (lib/active_state_index.sh:87)."""
    if not _table_exists(con, "policy_decisions"):
        return []
    cur = con.execute(
        """SELECT decision_id, run_id, event_type, policy_name, result, reason, evaluated_at
           FROM policy_decisions
           WHERE result IN ('DENY','REQUIRE_APPROVAL')
             AND evaluated_at >= (strftime('%s','now') - ? * 86400)
           ORDER BY evaluated_at DESC LIMIT ?""",
        (days, max_n),
    )
    out: list[dict[str, Any]] = []
    for r in cur:
        out.append({
            "decision_id": r["decision_id"],
            "run_id": r["run_id"],
            "policy_name": r["policy_name"],
            "result": r["result"],
            "reason": (r["reason"] or "")[:200],
            "evaluated_at": r["evaluated_at"],
        })
    return out


def _established_facts(
    con: sqlite3.Connection,
    max_n: int,
    task_class: str,
    days: int,
) -> list[dict[str, Any]]:
    """Mirror _mo_asi_established_facts (lib/active_state_index.sh:120).

    Uses ORDER BY ended_at DESC NULLS LAST (SQLite >= 3.30) — preserves
    bash heredoc verbatim per kickoff parity rule.
    """
    if not _table_exists(con, "task_runs"):
        return []
    cur = con.execute(
        """SELECT id, task_class, recipe, verdict, cost_usd, duration_ms, ended_at, notes
           FROM task_runs
           WHERE verdict = 'APPROVE'
             AND (? = '__any__' OR task_class = ?)
             AND COALESCE(ended_at, updated_at) >= (strftime('%s','now') - ? * 86400)
           ORDER BY ended_at DESC NULLS LAST LIMIT ?""",
        (task_class, task_class, days, max_n),
    )
    out: list[dict[str, Any]] = []
    for r in cur:
        out.append({
            "run_id": r["id"],
            "task_class": r["task_class"],
            "recipe": r["recipe"],
            "cost_usd": r["cost_usd"],
            "duration_ms": r["duration_ms"],
            "notes": (r["notes"] or "")[:200],
        })
    return out


def _pending_goals(
    con: sqlite3.Connection,
    max_n: int,
    task_class: str,
) -> list[dict[str, Any]]:
    """Mirror _mo_asi_pending_goals (lib/active_state_index.sh:155)."""
    if not _table_exists(con, "task_runs"):
        return []
    cur = con.execute(
        """SELECT id, task_class, recipe, status, kickoff_path, created_at
           FROM task_runs
           WHERE status NOT IN ('published','rolled_back','failed')
             AND (? = '__any__' OR task_class = ?)
           ORDER BY created_at DESC LIMIT ?""",
        (task_class, task_class, max_n),
    )
    out: list[dict[str, Any]] = []
    for r in cur:
        out.append({
            "run_id": r["id"],
            "task_class": r["task_class"],
            "recipe": r["recipe"],
            "status": r["status"],
            "kickoff_path": r["kickoff_path"],
        })
    return out


def _decision_variables() -> list[dict[str, str]]:
    """Static 6-entry list — verbatim mirror of bash heredoc."""
    return list(DECISION_VARIABLES)


def render_active_state_block(
    task_class: str = "__any__",
    days_window: int = 30,
    max_per_section: int | None = None,
    db_path: str | None = None,
    disabled: bool | None = None,
) -> str:
    """Render the active-state markdown block (HarnessBridge T1).

    Mirrors ``mo_active_state_block`` in lib/active_state_index.sh:
    short-circuits to "" when disabled, returns "" when total==0 AND
    len(decision_variables)==0 (unreachable with the static list, but
    preserved verbatim for parity), otherwise emits the markdown wrapper
    byte-equal to bash's print() sequence.

    Args:
        task_class: filter for established_facts + pending_goals. Pass
            "__any__" to disable the filter (matches bash default).
        days_window: time window for established_facts in days. Bash
            hardcodes 7 for unresolved_errors + open_constraints.
        max_per_section: cap per section. Reads from env
            MO_ACTIVE_STATE_MAX_PER_SECTION if None. Default 5.
        db_path: SQLite DB path. Reads from env MINI_ORK_DB if None.
        disabled: override MO_DISABLE_ACTIVE_STATE. If None, reads env.

    Returns:
        The rendered markdown block (with trailing newline), or "" when
        disabled or the empty-block shortcut fires.
    """
    if disabled is None:
        disabled = os.environ.get("MO_DISABLE_ACTIVE_STATE") == "1"
    if disabled:
        return ""

    days_window = int(days_window)
    if max_per_section is None:
        max_per_section = int(os.environ.get("MO_ACTIVE_STATE_MAX_PER_SECTION", "5"))
    else:
        max_per_section = int(max_per_section)

    if db_path is None:
        db_path = _default_db_path()

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        u = _unresolved_errors(con, max_per_section, 7)
        o = _open_constraints(con, max_per_section, 7)
        e = _established_facts(con, max_per_section, task_class, days_window)
        p = _pending_goals(con, max_per_section, task_class)
        d = _decision_variables()
    finally:
        con.close()

    total = len(u) + len(o) + len(e) + len(p)
    if total == 0 and len(d) == 0:
        return ""

    block = {
        "schema": "mini-ork.active-state-index/v1",
        "source": "state.db",
        "unresolved_errors": u,
        "open_constraints": o,
        "established_facts": e,
        "pending_goals": p,
        "decision_variables": d,
    }

    lines: list[str] = [
        "--- ACTIVE STATE INDEX (HarnessBridge T1) ---",
        "",
        "```json",
        json.dumps(block, indent=2, ensure_ascii=False),
        "```",
        "",
    ]
    counts: list[str] = []
    if u:
        counts.append(f"{len(u)} unresolved error{'s' if len(u) != 1 else ''}")
    if o:
        counts.append(f"{len(o)} open constraint{'s' if len(o) != 1 else ''}")
    if e:
        counts.append(f"{len(e)} established fact{'s' if len(e) != 1 else ''}")
    if p:
        counts.append(f"{len(p)} pending goal{'s' if len(p) != 1 else ''}")
    if counts:
        lines.append("**Summary:** " + ", ".join(counts) + ".")
    lines.append("--- /ACTIVE STATE INDEX ---")
    return "\n".join(lines) + "\n"