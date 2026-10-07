"""Overview tab — Needs you, Outcomes by class, and Class detail.

The operator's first question — "is mini-ork getting better at my work?" — answered
from ``task_runs``, ``bug_reports``, ``semantic_memory`` and ``promotion_records``.
Every section is bound-parameter; the ``now`` argument is injected so tests can
freeze the clock without ``freezegun``.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S
from mini_ork.ide_pages.learn._common import db, short

DAY = 86400
_PASS = {"published", "completed", "success"}
_FAIL = {"failed", "rolled_back", "error"}


def sections(home: Path, args: dict[str, str], errors: dict[str, str], *, now: int | None = None) -> list[dict[str, Any]]:
    moment = int(now) if now is not None else int(time.time())
    sections_out = S.guarded(errors, "Needs you", lambda: _needs_you(home, moment))
    sections_out += S.guarded(errors, "Outcomes by task class",
                              lambda: _outcomes_table(home, moment, args))
    cls = (args.get("cls") or "").strip()
    if cls:
        sections_out += S.guarded(errors, f"{cls} · last 10 finished runs",
                                  lambda: _class_detail(home, moment, cls))
    return sections_out


# ── Needs you ──────────────────────────────────────────────────────────────

def _needs_you(home: Path, now: int) -> dict[str, Any]:
    conn = db(home)
    chips: list[dict[str, Any]] = []

    # 1. Open bug reports
    open_bugs = 0
    if conn.has_table("bug_reports"):
        rows = conn.rows("SELECT COUNT(*) AS n FROM bug_reports WHERE status = 'open'")
        open_bugs = int(rows[0]["n"]) if rows else 0
    if open_bugs:
        chips.append({"t": f"{open_bugs} open bug report{'s' if open_bugs != 1 else ''}",
                     "on": False, "do": S.page_link("verify", "bugs")})

    # 2. Runs stuck in executing > 24h
    stuck = 0
    if conn.has_table("task_runs"):
        rows = conn.rows("SELECT COUNT(*) AS n FROM task_runs WHERE status = 'executing' "
                         "AND created_at < ?", (now - DAY,))
        stuck = int(rows[0]["n"]) if rows else 0
    if stuck:
        chips.append({"t": f"{stuck} run{'s' if stuck != 1 else ''} stuck in executing > 24h",
                     "on": False, "do": S.page_link("runs", None, filter="working")})

    # 3. Decaying memories — same rule as the lifecycle tab
    decaying = 0
    if conn.has_table("semantic_memory"):
        from mini_ork.memory import RETIRE_ENTER_UTILITY, RETIRE_MIN_USES

        cols = {r["name"] for r in conn.rows("PRAGMA table_info(semantic_memory)")}
        if {"uses", "wins", "retired_at"} <= cols:
            rows = conn.rows(
                "SELECT COUNT(*) AS n FROM semantic_memory "
                "WHERE retired_at = 0 AND uses >= ? AND (wins + 1.0) / (uses + 2.0) < ?",
                (RETIRE_MIN_USES, RETIRE_ENTER_UTILITY))
            decaying = int(rows[0]["n"]) if rows else 0
    if decaying:
        chips.append({"t": f"{decaying} decaying memor{'ies' if decaying != 1 else 'y'}",
                     "on": False, "do": S.page_link("learn", "memory")})

    # 4. Quarantined promotion decisions in the last 7 days
    quarantined = 0
    if conn.has_table("promotion_records"):
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 7 * DAY))
        rows = conn.rows("SELECT COUNT(*) AS n FROM promotion_records "
                         "WHERE decision = 'quarantined' AND decided_at >= ?", (now_iso,))
        quarantined = int(rows[0]["n"]) if rows else 0
    if quarantined:
        chips.append({"t": f"{quarantined} promotion{'s' if quarantined != 1 else ''} quarantined in the last 7 days",
                     "on": False, "do": S.page_link("learn", "improve")})

    if not chips:
        return S.lst("Needs you", [S.ok("Nothing needs you")])
    return S.chips("Needs you", chips, full=True,
                   note="Items are non-zero counts only — zero means no attention is needed.")


# ── Outcomes by task class ─────────────────────────────────────────────────

def _outcomes_table(home: Path, now: int, args: dict[str, str]) -> dict[str, Any]:
    conn = db(home)
    if not conn.has_table("task_runs"):
        return S.table("Outcomes by task class",
                       [S.col(140), S.col(60), S.col(80), S.col(80), S.col(80), S.col(80), S.col(60)],
                       ["class", "runs", "pass rate", "Δ pass", "cost / pass", "Δ cost", "stuck"],
                       [[S.muted("No task_runs table — outcomes cannot be computed"), "", "",
                         "", "", "", ""]],
                       full=True,
                       note="Last 28 days vs the 28 before. Pass = published; fail = failed. "
                            "Runs still executing after 24 h are counted as stuck, not as failures.")

    cur_start = now - 28 * DAY
    base_start = now - 56 * DAY
    stuck_cutoff = now - DAY

    rows = conn.rows("SELECT task_class, status, created_at, cost_usd FROM task_runs "
                     "WHERE created_at >= ?", (base_start,))
    by_class: dict[str, dict[str, Any]] = {}
    for r in rows:
        cls = str(r.get("task_class") or "unknown")
        status = str(r.get("status") or "")
        ts = int(r.get("created_at") or 0)
        cost = float(r.get("cost_usd") or 0)
        is_pass = status in _PASS
        is_fail = status in _FAIL

        if not (is_pass or is_fail):
            # In-flight or otherwise non-terminal: count stuck once we know the class
            # exists. Use a default-zero aggregator so the empty state stays
            # reachable when every row is non-terminal.
            if status == "executing" and ts < stuck_cutoff:
                agg = by_class.setdefault(cls, {"cur_pass": 0, "cur_fail": 0, "cur_cost": 0.0, "cur_stuck": 0,
                                                 "base_pass": 0, "base_fail": 0, "base_cost": 0.0})
                agg["cur_stuck"] += 1
            continue

        agg = by_class.setdefault(cls, {"cur_pass": 0, "cur_fail": 0, "cur_cost": 0.0, "cur_stuck": 0,
                                         "base_pass": 0, "base_fail": 0, "base_cost": 0.0})
        # Accumulate cost on every terminal row (pass and fail); cost/pass divides
        # by passes at read time so failed runs surface their real spend.
        if ts >= cur_start:
            if is_pass:
                agg["cur_pass"] += 1
            else:
                agg["cur_fail"] += 1
            agg["cur_cost"] += cost
        else:
            if is_pass:
                agg["base_pass"] += 1
            else:
                agg["base_fail"] += 1
            agg["base_cost"] += cost

    # Top 10 classes by terminal runs in current window.
    ranked = sorted(by_class.items(),
                    key=lambda kv: -(kv[1]["cur_pass"] + kv[1]["cur_fail"]))[:10]
    if not ranked:
        return S.table("Outcomes by task class",
                       [S.col(140), S.col(60), S.col(80), S.col(80), S.col(80), S.col(80), S.col(60)],
                       ["class", "runs", "pass rate", "Δ pass", "cost / pass", "Δ cost", "stuck"],
                       [[S.muted("No finished runs in the last 56 days."), "", "", "", "", "", ""]],
                       full=True,
                       note="Last 28 days vs the 28 before. Pass = published; fail = failed. "
                            "Runs still executing after 24 h are counted as stuck, not as failures.")

    cols = [S.col(160), S.col(60), S.col(80), S.col(80), S.col(90), S.col(80), S.col(60)]
    head = ["class", "runs", "pass rate", "Δ pass", "cost / pass", "Δ cost", "stuck"]
    sel = (args.get("cls") or "").strip()
    out_rows: list[dict[str, Any]] = []
    for cls, agg in ranked:
        cur_runs = agg["cur_pass"] + agg["cur_fail"]
        base_runs = agg["base_pass"] + agg["base_fail"]
        cur_rate: float | None = (100 * agg["cur_pass"] / cur_runs) if cur_runs >= 5 else None
        base_rate: float | None = (100 * agg["base_pass"] / base_runs) if base_runs >= 5 else None
        d_pass: float | None = (cur_rate - base_rate) if cur_rate is not None and base_rate is not None else None
        cur_cpp: float | None = (agg["cur_cost"] / agg["cur_pass"]) if agg["cur_pass"] else None
        base_cpp: float | None = (agg["base_cost"] / agg["base_pass"]) if agg["base_pass"] else None
        d_cost_pct: float | None = (100 * (cur_cpp - base_cpp) / base_cpp
                                    if cur_cpp is not None and base_cpp is not None and base_cpp > 0
                                    else None)

        # Each Δ cell colours from its own metric. Sharing one trend flag made
        # a -20pt pass drop look green whenever cost fell, hiding regressions.
        d_pass_color = "muted"
        if d_pass is not None:
            if d_pass >= 5:
                d_pass_color = "green"
            elif d_pass <= -5:
                d_pass_color = "red"
        d_cost_color = "muted"
        if d_cost_pct is not None:
            if d_cost_pct <= -15:
                d_cost_color = "green"
            elif d_cost_pct >= 15:
                d_cost_color = "red"

        if cur_rate is None:
            rate_text = "— (n<5)"
            rate_color = "sub"
        elif cur_rate >= 70:
            rate_text = f"{cur_rate:.0f}%"; rate_color = "green"
        elif cur_rate < 50:
            rate_text = f"{cur_rate:.0f}%"; rate_color = "red"
        else:
            rate_text = f"{cur_rate:.0f}%"; rate_color = "yellow"

        d_pass_text = f"{d_pass:+.0f}pt" if d_pass is not None else "—"
        cpp_text = S.money(cur_cpp) if cur_cpp is not None else "—"
        d_cost_text = f"{d_cost_pct:+.0f}%" if d_cost_pct is not None else "—"

        out_rows.append({"cells": [
            S.cell(cls, "text"),
            S.mono(str(cur_runs)),
            S.cell(rate_text, rate_color),
            S.cell(d_pass_text, d_pass_color),
            cpp_text if isinstance(cpp_text, dict) else S.mono(cpp_text),
            S.cell(d_cost_text, d_cost_color),
            S.mono(str(agg["cur_stuck"])),
        ], "do": S.set_args(cls=cls), "sel": cls == sel})

    return S.table("Outcomes by task class", cols, head, out_rows, full=True,
                   note="Last 28 days vs the 28 before. Pass = published; fail = failed. "
                        "Runs still executing after 24 h are counted as stuck, not as failures.")


# ── Class detail ───────────────────────────────────────────────────────────

def _class_detail(home: Path, now: int, cls: str) -> dict[str, Any]:
    conn = db(home)
    if not conn.has_table("task_runs"):
        return S.table(f"{cls} · last 10 finished runs",
                       [S.col(80), S.col(fr=1, min=140), S.col(70), S.col(60), S.col(fr=1, min=160)],
                       ["status", "run", "cost", "age", "failure"],
                       [[S.muted("No task_runs table"), "", "", "", ""]])

    rows = conn.rows("SELECT id, status, cost_usd, ended_at, verdict FROM task_runs "
                     "WHERE task_class = ? AND status IN (?, ?, ?, ?, ?, ?) "
                     "ORDER BY COALESCE(ended_at, created_at) DESC LIMIT 10",
                     (cls, *_PASS, *_FAIL))
    cols = [S.col(80), S.col(fr=1, min=160), S.col(70), S.col(60), S.col(fr=1, min=140)]
    head = ["status", "run", "cost", "age", "failure"]
    out_rows: list[Any] = []
    for r in rows:
        run_id = str(r.get("id") or "")
        status = str(r.get("status") or "")
        cost = float(r.get("cost_usd") or 0)
        ended_at = r.get("ended_at")
        verdict = r.get("verdict")
        # Best-effort failure lookup. failure_memory.run_id FKs to the older
        # ``runs`` table (INTEGER) while task_runs.id is TEXT, so we cross on
        # ``runs.run_dir`` LIKE '/<task_run_id>'. With live data this still
        # resolves to nothing — failure_memory isn't populated for task_runs —
        # so the column falls back to ``task_runs.verdict`` (DATA GAP).
        failure_text = ""
        if conn.has_table("failure_memory") and conn.has_table("runs"):
            f_rows = conn.rows(
                "SELECT f.failure_category, f.workflow_stage FROM failure_memory f "
                "JOIN runs r ON r.id = f.run_id AND r.run_dir LIKE '%' || ? || ? "
                "ORDER BY f.occurred_at DESC LIMIT 1", ("/", run_id))
            if f_rows:
                fr = f_rows[0]
                failure_text = f"{fr.get('failure_category') or 'unknown'} · {fr.get('workflow_stage') or '?'}"
        if not failure_text:
            failure_text = str(verdict or "—")
        age_text = S.age(ended_at, now) if ended_at else "—"
        out_rows.append({"cells": [
            S.cell(status, "green" if status in _PASS else "red"),
            S.cell(run_id, "text"),
            S.mono(S.money(cost)),
            S.muted(age_text),
            S.muted(short(failure_text, 140)),
        ], "do": S.open_run(run_id, run_id)})

    if not out_rows:
        out_rows.append([S.muted("—"), S.muted("No terminal runs for this class yet"), "", "", ""])
    return S.table(f"{cls} · last 10 finished runs", cols, head, out_rows, full=True,
                   note="Newest first. Failure column prefers failure_memory.failure_category "
                        "(cross-referenced on run_id), falls back to task_runs.verdict.")