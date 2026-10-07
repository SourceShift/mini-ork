"""Self-improve tab — Loop, Candidate, Ledger, Health probes, Idea tree and TraceOtter.

The Idea tree moved here from ``memory.py`` because it answers "what to harvest
next". TraceOtter is a single combined section titled
"Training data export · TraceOtter" with the flow + corpus + failure modes
nested under it.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S
from mini_ork.ide_pages.learn._common import count, db, outcome_colour, short


def sections(home: Path, args: dict[str, str], errors: dict[str, str]) -> list[dict[str, Any]]:
    return (S.guarded(errors, "Loop", lambda: _loop(home))
            + S.guarded(errors, "Candidate", lambda: _candidate(home))
            + S.guarded(errors, "Self-improve ledger", lambda: _ledger(home))
            + S.guarded(errors, "Health probes", lambda: _health(home))
            + S.guarded(errors, "Idea tree", lambda: _idea_tree(home))
            + S.guarded(errors, "Training data export · TraceOtter",
                        lambda: _traceotter_section(home)))


def _latest_promotion(conn) -> dict[str, Any] | None:
    if not conn.has_table("promotion_records"):
        return None
    rows = conn.rows("SELECT candidate_id, from_version_id, to_version_id, utility_before, utility_after, "
                     "decision, decided_at, decided_by, rationale FROM promotion_records "
                     "ORDER BY decided_at DESC LIMIT 1")
    return rows[0] if rows else None


def _loop(home: Path) -> dict[str, Any]:
    conn = db(home)
    week = int(time.time()) - 7 * 86400
    grads = conn.rows("SELECT COUNT(*) AS n FROM gradient_records WHERE created_at >= ?", (week,))[0]["n"] \
        if conn.has_table("gradient_records") else 0
    runs = conn.rows("SELECT COUNT(*) AS n FROM self_improve_runs WHERE started_at >= ?", (week,))[0]["n"] \
        if conn.has_table("self_improve_runs") else 0
    promo = _latest_promotion(conn)
    if promo:
        evaluated = f"utility {promo.get('utility_before')} → {promo.get('utility_after')}"
        decision = str(promo.get("decision") or "pending")
    else:
        evaluated, decision = "nothing evaluated yet", "no promotions yet"
    return S.flow("Loop", [
        ("reflect", f"{int(grads or 0):,} gradients this week", "green" if grads else "sub"),
        ("improve", f"{int(runs or 0)} self-improve iterations this week", "green" if runs else "sub"),
        ("eval", evaluated, "text" if promo else "sub"),
        ("promote", decision, outcome_colour(decision) if promo else "sub"),
    ], full=True)


def _candidate(home: Path) -> dict[str, Any]:
    promo = _latest_promotion(db(home))
    if not promo:
        return S.lst("Candidate", [S.dot("No candidate has been evaluated yet")], full=True)
    title = f"Candidate {promo.get('candidate_id')} vs {promo.get('from_version_id')}"
    return S.kv(title, [
        ("Utility", f"{promo.get('utility_before')} → {promo.get('utility_after')}"),
        ("Decision", promo.get("decision") or "pending", outcome_colour(promo.get("decision"))),
        ("Decided by", promo.get("decided_by") or "—"),
        ("Decided at", str(promo.get("decided_at") or "—")[:16]),
    ], full=True, note=short(promo.get("rationale"), 200))


def _ledger(home: Path) -> dict[str, Any]:
    conn = db(home)
    rows = conn.rows("SELECT run_id, iter, outcome, notes, started_at, finished_at FROM self_improve_runs "
                     "ORDER BY started_at DESC LIMIT 8") if conn.has_table("self_improve_runs") else []
    costs: dict[str, float] = {}
    if rows and conn.has_table("llm_calls"):
        ids = [r["run_id"] for r in rows]
        marks = ",".join("?" * len(ids))
        for c in conn.rows(f"SELECT run_id, SUM(cost_usd) AS usd FROM llm_calls WHERE run_id IN ({marks}) "  # noqa: S608
                           "GROUP BY run_id", ids):
            costs[str(c["run_id"])] = float(c.get("usd") or 0.0)
    cols = [S.col(40), S.col(90), S.col(fr=1, min=0), S.col(56), S.col(60)]
    head = ["iter", "outcome", "change", "cost", "wall"]
    out = []
    for r in rows:
        wall = (int(r["finished_at"]) - int(r["started_at"])) if r.get("finished_at") and r.get("started_at") else None
        out.append([S.mono(r.get("iter") or "—"), S.cell(r.get("outcome") or "running", outcome_colour(r.get("outcome"))),
                    short(r.get("notes") or r.get("run_id"), 90),
                    S.mono(S.money(costs[r["run_id"]]) if r["run_id"] in costs else "—"),
                    S.mono(S.duration(wall) if wall is not None else "—")])
    if not out:
        out = [[S.muted("—"), S.muted("no iterations yet"), "", "", ""]]
    return S.table("Self-improve ledger", cols, head, out,
                   actions=[S.btn("Start self-improve", None)],
                   note="Start it from a terminal: mini-ork self-improve runs for hours.")


def _health(home: Path) -> dict[str, Any]:
    conn = db(home)
    items = []
    collapse = conn.rows("SELECT task_class, step, score, created_at FROM collapse_history "
                         "ORDER BY created_at DESC LIMIT 1") if conn.has_table("collapse_history") else []
    if collapse:
        c = collapse[0]
        items.append(S.dot("collapse-check", f"last: {c.get('task_class')} step {c.get('step')} "
                                             f"score {c.get('score')}"))
    else:
        items.append(S.dot("collapse-check", "has not run here"))
    quarantined = conn.rows("SELECT COUNT(*) AS n FROM version_registry WHERE status = 'quarantined'")[0]["n"] \
        if conn.has_table("version_registry") else 0
    items.append((S.warn if quarantined else S.ok)("Quarantine", f"{int(quarantined or 0)} version(s) quarantined"))
    safety = conn.rows("SELECT COUNT(*) AS n FROM safety_events WHERE COALESCE(status,'open') = 'open'")[0]["n"] \
        if conn.has_table("safety_events") else 0
    items.append((S.bad if safety else S.ok)("RSP tripwires", f"{int(safety or 0)} open safety event(s)"))
    aborts = count(conn, "watchdog_aborts")
    items.append(S.dot("Watchdog", f"{aborts} abort(s) recorded"))
    return S.lst("Health probes", items)


def _idea_tree(home: Path) -> dict[str, Any]:
    conn = db(home)
    if not conn.has_table("idea_tree_nodes"):
        return S.lst("Idea tree", [S.dot("No idea tree yet")])
    roots = conn.rows("SELECT node_id, hypothesis, status FROM idea_tree_nodes "
                      "WHERE parent_node_id IS NULL OR parent_node_id = '' ORDER BY created_at DESC LIMIT 1")
    if not roots:
        return S.lst("Idea tree", [S.dot("No idea tree yet")])
    root = roots[0]
    nodes = conn.rows("SELECT node_id, parent_node_id, hypothesis, status FROM idea_tree_nodes "
                      "WHERE root_node_id = ? ORDER BY created_at", (root["node_id"],))
    children: dict[str, list[dict[str, Any]]] = {}
    for n in nodes:
        if n["node_id"] != root["node_id"]:
            children.setdefault(str(n.get("parent_node_id") or root["node_id"]), []).append(n)
    lines: list[tuple[str, str]] = [(f"root  {short(root.get('hypothesis'), 70)}", "text")]

    def walk(parent: str, prefix: str, depth: int) -> None:
        kids = children.get(parent, [])
        for i, n in enumerate(kids):
            if len(lines) >= 14:
                return
            last = i == len(kids) - 1
            status = str(n.get("status") or "")
            lines.append((f"{prefix}{'└─' if last else '├─'} {short(n.get('hypothesis'), 60)}   {status}",
                          {"green": "green", "red": "red"}.get(outcome_colour(status), "muted")))
            if depth < 3:
                walk(n["node_id"], prefix + ("   " if last else "│  "), depth + 1)

    walk(root["node_id"], "", 0)
    if len(nodes) > len(lines):
        lines.append((f"… {len(nodes) - len(lines)} more nodes", "dim"))
    return S.code(f"Idea tree · {short(root.get('hypothesis'), 40)}", lines)


def _traceotter_section(home: Path) -> list[dict[str, Any]]:
    """TraceOtter rendered as one named section with its flow, corpus and failure modes nested.

    The kickoff mandates a single ``Training data export · TraceOtter`` title
    so the table of contents groups the three sub-sections under one banner.
    Returns a list — guarded wraps the per-title errors.
    """
    from mini_ork.web.routes.traceotter import summary

    data = summary(home=home)
    ingest = S.btn("Ingest", S.cli("traceotter", confirm="Distill this project's runs with TraceOtter now?",
                                   home=False))
    if not data.get("available"):
        return [
            S.flow("Training data export · TraceOtter",
                   [("ingest", "Codex · Claude · mini-ork"), ("normalize", "ADP episodes"),
                    ("consolidate", "procedural skills"), ("export", "LLaMA-Factory SFT")],
                   full=True),
            S.lst("Corpus", [S.dot("TraceOtter has not run here", str(data.get("reason") or ""))],
                  full=True, actions=[ingest]),
        ]
    report = home / "traceotter" / "report.json"
    last = S.age(report.stat().st_mtime, int(time.time())) + " ago" if report.is_file() else "—"
    episodes = int(data.get("episodes") or 0)
    flow = S.flow("Training data export · TraceOtter", [
        ("ingest", f"{episodes:,} trajectories"),
        ("normalize", f"{int(data.get('should_imitate') or 0):,} worth imitating"),
        ("consolidate", f"{int(data.get('skills') or 0)} procedural skills"),
        ("export", f"{int(data.get('sft_examples') or 0):,} LLaMA-Factory SFT examples"),
    ], full=True)
    corpus = S.kv("Corpus", [
        ("Episodes", f"{episodes:,}"), ("Skills", str(int(data.get("skills") or 0))),
        ("SFT examples", f"{int(data.get('sft_examples') or 0):,}"), ("Last ingest", last),
    ], full=True, actions=[ingest])
    out = [flow, corpus]
    modes = data.get("failure_modes") or []
    if modes:
        out.append(S.lst("Failure modes the distiller saw",
                         [S.warn(m.get("mode"), f"{m.get('count')} episodes") for m in modes[:6]],
                         full=True))
    return out