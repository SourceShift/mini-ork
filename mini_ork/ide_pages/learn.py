"""Learning & memory — what runs leave behind, what the next run is given, how the pipeline changes itself."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S

TITLE = "Learning & memory"
SUB = ("What runs leave behind, what the next run is given, "
       "and how the pipeline changes itself under gates.")
TABS = [("learnings", "Learnings"), ("memory", "Memory"), ("improve", "Self-improve"),
        ("bugs", "Bug reports"), ("traceotter", "TraceOtter")]

# The state.db namespaces the design's "Namespaces" bars count.
_NAMESPACES = [("task", "task_memory"), ("workflow", "workflow_memory"),
               ("agent_performance", "agent_performance_memory"), ("failure", "failure_memory"),
               ("recovery", "recovery_memory"), ("user_preference", "user_preference_memory"),
               ("artifact", "artifact_memory"), ("benchmark", "benchmark_memory")]
_GOOD = {"success", "promoted", "adopted", "harvested", "accepted", "approved"}
_BAD = {"rejected", "failed", "timed_out", "refuted", "pruned", "quarantined", "error"}


def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    tab = tab if tab in dict(TABS) else TABS[0][0]
    errors: dict[str, str] = {}
    g = lambda title, fn: S.guarded(errors, title, fn)  # noqa: E731
    if tab == "learnings":
        sections = (g("Gradients", lambda: _gradients(home)) + g("Patterns", lambda: _patterns(home))
                    + g("Failure modes", lambda: _failure_modes(home)))
    elif tab == "memory":
        sections = (g("Namespaces · state.db", lambda: _namespaces(home))
                    + g("Semantic memory lifecycle", lambda: _lifecycle(home))
                    + g("Idea tree", lambda: _idea_tree(home)))
    elif tab == "improve":
        sections = (g("Loop", lambda: _loop(home)) + g("Candidate", lambda: _candidate(home))
                    + g("Self-improve ledger", lambda: _ledger(home))
                    + g("Health probes", lambda: _health(home)))
    elif tab == "bugs":
        sections = g("Bug reports", lambda: _bugs(home))
    else:
        sections = g("TraceOtter", lambda: _traceotter(home))
    return S.page("learn", TITLE, SUB, chips_=[], actions=[], tabs=TABS, tab=tab, args=args,
                  sections=sections, errors=errors)


def _db(home: Path):
    from mini_ork.web.deps import db_for

    return db_for(home)


def _count(db, table: str) -> int:
    if not db.has_table(table):
        return 0
    rows = db.rows(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608 — fixed table names
    return int(rows[0]["n"]) if rows else 0


def _short(text: Any, limit: int = 120) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= limit else t[: limit - 1] + "…"


def _outcome_colour(value: Any) -> str:
    v = str(value or "").lower()
    if v in _GOOD:
        return "green"
    if v in _BAD:
        return "red"
    return "yellow" if v else "sub"


# ── Learnings ──────────────────────────────────────────────────────────────

def _gradients(home: Path) -> dict[str, Any]:
    db = _db(home)
    cols = [S.col(fr=1, min=0), S.col(90), S.col(44), S.col(56)]
    head = ["gradient", "task class", "conf", "injected"]
    rows = db.rows("SELECT gradient_id, target, signal, suggested_change, confidence, task_class "
                   "FROM gradient_records ORDER BY created_at DESC LIMIT 15") \
        if db.has_table("gradient_records") else []
    out = [[S.cell(_short(f"{r.get('target') or 'gradient'} · {r.get('signal') or r.get('suggested_change')}"),
                   "text"),
            S.muted(r.get("task_class") or "any"),
            S.mono(f"{float(r.get('confidence') or 0):.2f}"),
            S.muted("—")] for r in rows]
    if not out:
        out = [[S.muted("No gradients yet — reflection writes them as runs finish"), "", "", ""]]
    total = _count(db, "gradient_records")
    return S.table("Gradients", cols, head, out, full=True,
                   note=f"Reflection turns traces into natural-language gradients ({total:,} recorded, "
                        "newest first). High-confidence ones are injected into node prompts at dispatch; "
                        "injection is not counted per gradient.")


def _patterns(home: Path) -> dict[str, Any]:
    from mini_ork.web.routes.learning import emergent_patterns

    rows = emergent_patterns(_db(home), 8)
    if not rows:
        return S.lst("Patterns", [S.dot("No patterns yet")])
    return S.lst("Patterns", [
        S.dot(_short(r.get("cluster_label") or r.get("pattern_id"), 80),
              f"strength {float(r.get('strength_score') or 0):.2f} · {r.get('status') or 'open'}"
              + (f" · {_short(r.get('suggested_meta_adr'), 90)}" if r.get("suggested_meta_adr") else ""))
        for r in rows])


def _failure_modes(home: Path) -> dict[str, Any]:
    """failure_memory grouped by category and stage, with the recovery that worked if one is recorded."""
    db = _db(home)
    rows = db.rows(
        "SELECT failure_category AS category, workflow_stage AS stage, COUNT(*) AS n, "
        "COUNT(DISTINCT run_id) AS runs, MAX(occurred_at) AS last, MAX(failure_id) AS sample "
        "FROM failure_memory GROUP BY failure_category, workflow_stage ORDER BY n DESC LIMIT 6") \
        if db.has_table("failure_memory") else []
    if not rows:
        return S.lst("Failure modes", [S.ok("No failure modes recorded")])
    recoveries: dict[str, str] = {}
    if db.has_table("recovery_memory"):
        for r in db.rows("SELECT f.failure_category AS category, m.recovery_action AS action "
                         "FROM recovery_memory m JOIN failure_memory f ON f.failure_id = m.failure_id "
                         "WHERE m.outcome = 'success' ORDER BY m.recovered_at DESC"):
            recoveries.setdefault(str(r.get("category")), str(r.get("action") or ""))
    items = []
    for r in rows:
        n, runs = int(r.get("n") or 0), int(r.get("runs") or 0)
        sub = f"{n} time{'s' if n != 1 else ''} in {runs} run{'s' if runs != 1 else ''} · last {str(r.get('last') or '—')[:10]}"
        if recoveries.get(str(r.get("category"))):
            sub += f" · recovery: {_short(recoveries[str(r.get('category'))], 60)}"
        mark = S.bad if n >= 3 else S.warn
        items.append(mark(f"{r.get('category') or 'unknown'} · {r.get('stage') or 'any stage'}", sub))
    return S.lst("Failure modes", items)


# ── Memory ─────────────────────────────────────────────────────────────────

def _namespaces(home: Path) -> dict[str, Any]:
    db = _db(home)
    counts = [(label, _count(db, table)) for label, table in _NAMESPACES]
    top = max((n for _, n in counts), default=0) or 1
    return S.bars("Namespaces · state.db", [(label, 100 * n / top, f"{n:,}") for label, n in counts],
                  full=True)


def _lifecycle(home: Path) -> dict[str, Any]:
    from mini_ork.memory import RETIRE_ENTER_UTILITY, RETIRE_MIN_USES

    db = _db(home)
    if not db.has_table("semantic_memory"):
        return S.kv("Semantic memory lifecycle", [("Active", "0"), ("Decaying", "0", "yellow"),
                                                  ("Retired", "0", "sub")])
    # The win/loss and retirement columns are added by mini_ork.memory.semantic on
    # first use; a store that never ran it has neither, so every row is active.
    cols = {r["name"] for r in db.rows("PRAGMA table_info(semantic_memory)")}
    retired = "COALESCE(retired_at,0)" if "retired_at" in cols else "0"
    weak = ("uses >= ? AND (wins + 1.0) / (uses + 2.0) < ?" if {"uses", "wins"} <= cols else "0 = 1")
    params = (RETIRE_MIN_USES, RETIRE_ENTER_UTILITY) if {"uses", "wins"} <= cols else ()
    row = db.rows(
        f"SELECT COUNT(*) AS total, SUM(CASE WHEN {retired} = 0 THEN 1 ELSE 0 END) AS active, "  # noqa: S608
        f"SUM(CASE WHEN {retired} = 0 AND {weak} THEN 1 ELSE 0 END) AS decaying, "
        f"SUM(CASE WHEN {retired} != 0 THEN 1 ELSE 0 END) AS retired FROM semantic_memory",
        params)[0]
    active, decaying = int(row.get("active") or 0), int(row.get("decaying") or 0)
    return S.kv("Semantic memory lifecycle", [
        ("Active", f"{active - decaying:,}"), ("Decaying", f"{decaying:,}", "yellow",
                                               "retirement candidates"),
        ("Retired", f"{int(row.get('retired') or 0):,}", "sub")],
        note="Decaying: active, used at least "
             f"{RETIRE_MIN_USES}×, win rate under {RETIRE_ENTER_UTILITY:.2f}. Nothing retires on its own.")


def _idea_tree(home: Path) -> dict[str, Any]:
    db = _db(home)
    if not db.has_table("idea_tree_nodes"):
        return S.lst("Idea tree", [S.dot("No idea tree yet")])
    roots = db.rows("SELECT node_id, hypothesis, status FROM idea_tree_nodes "
                    "WHERE parent_node_id IS NULL OR parent_node_id = '' ORDER BY created_at DESC LIMIT 1")
    if not roots:
        return S.lst("Idea tree", [S.dot("No idea tree yet")])
    root = roots[0]
    nodes = db.rows("SELECT node_id, parent_node_id, hypothesis, status FROM idea_tree_nodes "
                    "WHERE root_node_id = ? ORDER BY created_at", (root["node_id"],))
    children: dict[str, list[dict[str, Any]]] = {}
    for n in nodes:
        if n["node_id"] != root["node_id"]:
            children.setdefault(str(n.get("parent_node_id") or root["node_id"]), []).append(n)
    lines: list[tuple[str, str]] = [(f"root  {_short(root.get('hypothesis'), 70)}", "text")]

    def walk(parent: str, prefix: str, depth: int) -> None:
        kids = children.get(parent, [])
        for i, n in enumerate(kids):
            if len(lines) >= 14:
                return
            last = i == len(kids) - 1
            status = str(n.get("status") or "")
            lines.append((f"{prefix}{'└─' if last else '├─'} {_short(n.get('hypothesis'), 60)}   {status}",
                          {"green": "green", "red": "red"}.get(_outcome_colour(status), "muted")))
            if depth < 3:
                walk(n["node_id"], prefix + ("   " if last else "│  "), depth + 1)

    walk(root["node_id"], "", 0)
    if len(nodes) > len(lines):
        lines.append((f"… {len(nodes) - len(lines)} more nodes", "dim"))
    return S.code(f"Idea tree · {_short(root.get('hypothesis'), 40)}", lines)


# ── Self-improve ───────────────────────────────────────────────────────────

def _latest_promotion(db) -> dict[str, Any] | None:
    if not db.has_table("promotion_records"):
        return None
    rows = db.rows("SELECT candidate_id, from_version_id, to_version_id, utility_before, utility_after, "
                   "decision, decided_at, decided_by, rationale FROM promotion_records "
                   "ORDER BY decided_at DESC LIMIT 1")
    return rows[0] if rows else None


def _loop(home: Path) -> dict[str, Any]:
    db = _db(home)
    week = int(time.time()) - 7 * 86400
    grads = db.rows("SELECT COUNT(*) AS n FROM gradient_records WHERE created_at >= ?", (week,))[0]["n"] \
        if db.has_table("gradient_records") else 0
    runs = db.rows("SELECT COUNT(*) AS n FROM self_improve_runs WHERE started_at >= ?", (week,))[0]["n"] \
        if db.has_table("self_improve_runs") else 0
    promo = _latest_promotion(db)
    if promo:
        evaluated = f"utility {promo.get('utility_before')} → {promo.get('utility_after')}"
        decision = str(promo.get("decision") or "pending")
    else:
        evaluated, decision = "nothing evaluated yet", "no promotions yet"
    return S.flow("Loop", [
        ("reflect", f"{int(grads or 0):,} gradients this week", "green" if grads else "sub"),
        ("improve", f"{int(runs or 0)} self-improve iterations this week", "green" if runs else "sub"),
        ("eval", evaluated, "text" if promo else "sub"),
        ("promote", decision, _outcome_colour(decision) if promo else "sub"),
    ], full=True)


def _candidate(home: Path) -> dict[str, Any]:
    promo = _latest_promotion(_db(home))
    if not promo:
        return S.lst("Candidate", [S.dot("No candidate has been evaluated yet")], full=True)
    title = f"Candidate {promo.get('candidate_id')} vs {promo.get('from_version_id')}"
    return S.kv(title, [
        ("Utility", f"{promo.get('utility_before')} → {promo.get('utility_after')}"),
        ("Decision", promo.get("decision") or "pending", _outcome_colour(promo.get("decision"))),
        ("Decided by", promo.get("decided_by") or "—"),
        ("Decided at", str(promo.get("decided_at") or "—")[:16]),
    ], full=True, note=_short(promo.get("rationale"), 200))


def _ledger(home: Path) -> dict[str, Any]:
    db = _db(home)
    rows = db.rows("SELECT run_id, iter, outcome, notes, started_at, finished_at FROM self_improve_runs "
                   "ORDER BY started_at DESC LIMIT 8") if db.has_table("self_improve_runs") else []
    costs: dict[str, float] = {}
    if rows and db.has_table("llm_calls"):
        ids = [r["run_id"] for r in rows]
        marks = ",".join("?" * len(ids))
        for c in db.rows(f"SELECT run_id, SUM(cost_usd) AS usd FROM llm_calls WHERE run_id IN ({marks}) "  # noqa: S608
                         "GROUP BY run_id", ids):
            costs[str(c["run_id"])] = float(c.get("usd") or 0.0)
    cols = [S.col(40), S.col(90), S.col(fr=1, min=0), S.col(56), S.col(60)]
    head = ["iter", "outcome", "change", "cost", "wall"]
    out = []
    for r in rows:
        wall = (int(r["finished_at"]) - int(r["started_at"])) if r.get("finished_at") and r.get("started_at") else None
        out.append([S.mono(r.get("iter") or "—"), S.cell(r.get("outcome") or "running", _outcome_colour(r.get("outcome"))),
                    _short(r.get("notes") or r.get("run_id"), 90),
                    S.mono(S.money(costs[r["run_id"]]) if r["run_id"] in costs else "—"),
                    S.mono(S.duration(wall) if wall is not None else "—")])
    if not out:
        out = [[S.muted("—"), S.muted("no iterations yet"), "", "", ""]]
    return S.table("Self-improve ledger", cols, head, out,
                   actions=[S.btn("Start self-improve", None)],
                   note="Start it from a terminal: mini-ork self-improve runs for hours.")


def _health(home: Path) -> dict[str, Any]:
    db = _db(home)
    items = []
    collapse = db.rows("SELECT task_class, step, score, created_at FROM collapse_history "
                       "ORDER BY created_at DESC LIMIT 1") if db.has_table("collapse_history") else []
    if collapse:
        c = collapse[0]
        items.append(S.dot("collapse-check", f"last: {c.get('task_class')} step {c.get('step')} "
                                             f"score {c.get('score')}"))
    else:
        items.append(S.dot("collapse-check", "has not run here"))
    quarantined = db.rows("SELECT COUNT(*) AS n FROM version_registry WHERE status = 'quarantined'")[0]["n"] \
        if db.has_table("version_registry") else 0
    items.append((S.warn if quarantined else S.ok)("Quarantine", f"{int(quarantined or 0)} version(s) quarantined"))
    safety = db.rows("SELECT COUNT(*) AS n FROM safety_events WHERE COALESCE(status,'open') = 'open'")[0]["n"] \
        if db.has_table("safety_events") else 0
    items.append((S.bad if safety else S.ok)("RSP tripwires", f"{int(safety or 0)} open safety event(s)"))
    aborts = _count(db, "watchdog_aborts")
    items.append(S.dot("Watchdog", f"{aborts} abort(s) recorded"))
    return S.lst("Health probes", items)


# ── Bug reports ────────────────────────────────────────────────────────────

def _bugs(home: Path) -> dict[str, Any]:
    db = _db(home)
    rows = db.rows("SELECT id, title, observed_in, confidence, status, severity FROM bug_reports "
                   "ORDER BY CASE WHEN status = 'open' THEN 0 ELSE 1 END, confidence DESC LIMIT 20") \
        if db.has_table("bug_reports") else []
    cols = [S.col(60), S.col(fr=1, min=0), S.col(70), S.col(56)]
    out = [[S.mono(f"b-{r['id']}"), S.cell(_short(r.get("title"), 110), "text"),
            S.muted(Path(str(r.get("observed_in") or "—")).name), S.mono(f"{float(r.get('confidence') or 0):.2f}")]
           for r in rows]
    if not out:
        out = [[S.muted("—"), S.muted("No bug reports yet"), "", ""]]
    return S.table("Bug reports", cols, ["id", "report", "source", "score"], out, full=True,
                   note="Sweep runs for bug reports, prioritise, and promote the top ones into kickoffs.",
                   actions=[S.btn("Sweep runs", S.cli("bugs", "sweep", home=False)),
                            S.btn("Promote top 3", S.cli("bugs", "promote", "--top", "3",
                                                         confirm="Write kickoffs for the top 3 bug reports?",
                                                         home=False),
                                  "primary")])


# ── TraceOtter ─────────────────────────────────────────────────────────────

def _traceotter(home: Path) -> list[dict[str, Any]]:
    from mini_ork.web.routes.traceotter import summary

    data = summary(home=home)
    ingest = S.btn("Ingest", S.cli("traceotter", confirm="Distill this project's runs with TraceOtter now?", home=False))
    if not data.get("available"):
        return [S.flow("Pipeline", [("ingest", "Codex · Claude · mini-ork"), ("normalize", "ADP episodes"),
                                    ("consolidate", "procedural skills"), ("export", "LLaMA-Factory SFT")],
                       full=True),
                S.lst("Corpus", [S.dot("TraceOtter has not run here", str(data.get("reason") or ""))],
                      full=True, actions=[ingest])]
    report = home / "traceotter" / "report.json"
    last = S.age(report.stat().st_mtime, int(time.time())) + " ago" if report.is_file() else "—"
    episodes = int(data.get("episodes") or 0)
    flow = S.flow("Pipeline", [
        ("ingest", f"{episodes:,} trajectories"),
        ("normalize", f"{int(data.get('should_imitate') or 0):,} worth imitating"),
        ("consolidate", f"{int(data.get('skills') or 0)} procedural skills"),
        ("export", f"{int(data.get('sft_examples') or 0):,} LLaMA-Factory SFT examples"),
    ], full=True)
    corpus = S.kv("Corpus", [
        ("Episodes", f"{episodes:,}"), ("Skills", str(int(data.get("skills") or 0))),
        ("SFT examples", f"{int(data.get('sft_examples') or 0):,}"), ("Last ingest", last),
    ], full=True, actions=[ingest])
    modes = data.get("failure_modes") or []
    out = [flow, corpus]
    if modes:
        out.append(S.lst("Failure modes the distiller saw",
                         [S.warn(m.get("mode"), f"{m.get('count')} episodes") for m in modes[:6]], full=True))
    return out
