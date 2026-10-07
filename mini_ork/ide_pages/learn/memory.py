"""Memory tab — what mini-ork knows about you, which lane fits which work, what to forget.

The page replaces the legacy "namespaces bars + lifecycle kv" pair (which answered no
operator question) with three sections that answer three concrete ones:

1. **Preferences & constraints** — every operator-set preference with the count of
   nodes it was injected into in the last 7 days, plus an Add button.
2. **Lane fit by task class** — for the top task classes, which (role, model) lanes
   are worth choosing (pass rate + cost / run), with ★ on the best lane per class.
3. **Memories to review** — semantic memories whose per-memory win rate sits
   meaningfully below their scope baseline, plus every hysteresis candidate from
   ``mini_ork.memory.candidates()``.

Each section is wrapped in :func:`spec.guarded` so a single broken data source
costs one section, not the whole tab. The ``_namespaces`` and ``lifecycle kv``
helpers from the prior design are gone; ``_NAMESPACES`` itself stays in
``_common.py`` for callers and tests outside this module.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S
from mini_ork.ide_pages.learn._common import db, short


_PREF_SECTION_TITLE = "Preferences & constraints"
_PREF_NOTE = ("Every researcher, implementer and reviewer gets these first in its prompt. "
              "Add from a terminal: mini-ork prefs set <key> <text> "
              "[--scope task_class --target <class>].")
_PREF_ADD_THREAD = ("I want to add a mini-ork preference. Ask me what it is and its scope, "
                    "then run `mini-ork prefs set` for it.")

_LANE_NOTE = ("From agent_performance_memory (refreshed by reflect). "
              "★ = best pass rate with at least 5 runs.")

_MEMORY_KV_TITLE = "Memories to review · counts"
_MEMORY_NOTE = ("Win = the run it was used in succeeded. Compared with the scope baseline, "
                "because most runs succeed either way. True per-lesson lift comes with the 10% holdout.")

_RETIRE_REASON = "operator request from Memory tab"


def sections(home: Path, args: dict[str, str], errors: dict[str, str],  # noqa: ARG001
             *, now: int | None = None) -> list[dict[str, Any]]:  # noqa: ARG001
    """Compose the three Memory tab sections in the kickoff's order.

    ``args`` is unused today (no per-page filter yet on the Memory tab); it stays
    in the signature so the dispatcher in ``learn/__init__.py`` calls every
    tab with the same shape. ``now`` is exposed so tests can freeze time
    without freezegun; helpers that need it read it directly from
    ``time.time()`` and stay free of plumbing.
    """
    del args
    del now
    return (S.guarded(errors, _PREF_SECTION_TITLE, lambda: _preferences(home))
            + S.guarded(errors, "Lane fit by task class", lambda: _lane_fit(home))
            + S.guarded(errors, "Memories to review", lambda: _memories_to_review(home)))


# ── 1. Preferences & constraints ─────────────────────────────────────────


def _preferences(home: Path) -> dict[str, Any]:
    db_path = str(home / "state.db")
    prefs = _safe_list_prefs(db_path)
    items = []
    for p in prefs:
        title = short(p.get("value") or "", 200)
        scope = str(p.get("scope") or "global")
        target = str(p.get("target") or "")
        if scope == "global":
            scope_label = "global"
        else:
            scope_label = f"{scope}: {target}"
        sub_parts = [scope_label]
        sub_parts.append(_injection_sub(scope, target, str(p.get("key") or ""), p, db_path))
        src = str(p.get("source") or "")
        if src.startswith("file:"):
            fname = src[len("file:"):]
            sub_parts.append(f"from {fname}")
        sub = " · ".join(sub_parts)
        acts = _pref_acts(p)
        items.append(S.dot(title, sub, acts=acts))
    actions = [S.btn("Add preference", S.thread(_PREF_ADD_THREAD), "primary")]
    if not items:
        items = [S.dot("No preferences yet",
                       "Add one: mini-ork prefs set tone \"Keep summaries short\"")]
    return S.lst(_PREF_SECTION_TITLE, items, actions=actions, full=True,
                 note=_PREF_NOTE)


def _safe_list_prefs(db_path: str) -> list[dict]:
    """Read prefs but never raise on a missing DB / file. Empty list on failure."""
    try:
        from mini_ork.memory.preferences import list_prefs
        return list_prefs(db=db_path)
    except Exception:  # noqa: BLE001 — guarded: a broken source must not blank the page
        return []


def _injection_sub(scope: str, target: str, key: str, pref: dict, db_path: str) -> str:
    """The "given to N node(s) in 7 days" / "not given ..." sub-fragment."""
    src = str(pref.get("source") or "")
    if not src.startswith("db"):
        # File-sourced prefs never reach the injection ledger.
        return "not given to any node in 7 days"
    now = int(time.time())
    since = now - 7 * 86400
    source_id = f"pref:{scope}:{target}:{key}"
    try:
        from mini_ork.learning.ledger import injection_counts
        counts = injection_counts("preference", [source_id], since=since, db=db_path)
    except Exception:  # noqa: BLE001 — silent ledger failure → fall through to "not given"
        return "not given to any node in 7 days"
    info = (counts or {}).get(source_id) or {}
    uses = int(info.get("uses") or 0)
    if uses <= 0:
        return "not given to any node in 7 days"
    return f"given to {uses} node(s) in 7 days"


def _pref_acts(p: dict) -> list[dict[str, Any]]:
    src = str(p.get("source") or "")
    key = str(p.get("key") or "")
    scope = str(p.get("scope") or "global")
    target = str(p.get("target") or "")
    if src.startswith("file:"):
        return [S.btn("Open", S.open_path(src[len("file:"):]), "ghost")]
    return [S.btn(
        "Remove",
        S.cli("prefs", "rm", key, "--scope", scope, "--target", target,
              confirm=f"Remove preference '{key}'?"),
        "ghost",
    )]


# ── 2. Lane fit by task class ────────────────────────────────────────────


def _lane_fit(home: Path) -> dict[str, Any]:
    conn = db(home)
    if not conn.has_table("agent_performance_memory"):
        return S.table(
            "Lane fit by task class",
            [S.col(140), S.col(110), S.col(180), S.col(60), S.col(80), S.col(80)],
            ["class", "role", "lane", "runs", "pass rate", "cost / run"],
            [[S.muted("No lane history yet — it fills as runs finish"), "", "", "", "", ""]],
            actions=[S.btn("Lanes & cost", S.page_link("lanes"), "ghost")],
            full=True, note=_LANE_NOTE,
        )

    rows = conn.rows(
        "SELECT agent_version_id, role, model, task_class, runs_count, "
        "success_count, avg_cost_usd FROM agent_performance_memory "
        "WHERE runs_count >= 3"
    )
    by_class: dict[str, list[dict]] = {}
    for r in rows:
        by_class.setdefault(str(r.get("task_class") or "unknown"), []).append(r)

    ranked = sorted(by_class.items(),
                    key=lambda kv: -sum(int(x.get("runs_count") or 0) for x in kv[1]))[:8]
    if not ranked:
        return S.table(
            "Lane fit by task class",
            [S.col(140), S.col(110), S.col(180), S.col(60), S.col(80), S.col(80)],
            ["class", "role", "lane", "runs", "pass rate", "cost / run"],
            [[S.muted("No lane history yet — it fills as runs finish"), "", "", "", "", ""]],
            actions=[S.btn("Lanes & cost", S.page_link("lanes"), "ghost")],
            full=True, note=_LANE_NOTE,
        )

    cols = [S.col(140), S.col(110), S.col(180), S.col(60), S.col(80), S.col(80)]
    head = ["class", "role", "lane", "runs", "pass rate", "cost / run"]
    out: list[Any] = []
    for cls, lanes in ranked:
        # Top-4 lanes per class by pass rate desc; ties broken by runs desc.
        lanes_sorted = sorted(
            lanes,
            key=lambda r: (-(int(r.get("success_count") or 0)
                            / max(1, int(r.get("runs_count") or 0))),
                           -int(r.get("runs_count") or 0)),
        )[:4]
        # Best lane for the class — highest pass rate with runs >= 5.
        eligible = [r for r in lanes_sorted if int(r.get("runs_count") or 0) >= 5]
        if eligible:
            best_id = max(
                eligible,
                key=lambda r: int(r.get("success_count") or 0)
                              / max(1, int(r.get("runs_count") or 0)),
            ).get("agent_version_id")
        else:
            best_id = None
        first = True
        for r in lanes_sorted:
            runs = int(r.get("runs_count") or 0)
            succ = int(r.get("success_count") or 0)
            rate = (100.0 * succ / runs) if runs else 0.0
            rate_text = f"{rate:.0f}%"
            rate_color = _pass_rate_colour(rate)
            cost = float(r.get("avg_cost_usd") or 0.0)
            lane = str(r.get("agent_version_id") or "?")
            if r.get("agent_version_id") == best_id:
                lane = f"{lane} ★"
            out.append({
                "cells": [
                    S.cell(cls, "text") if first else S.muted(""),
                    S.cell(str(r.get("role") or ""), "text"),
                    S.mono(lane),
                    S.mono(str(runs)),
                    S.cell(rate_text, rate_color),
                    S.mono(S.money(cost)),
                ],
                "do": None,
                "sel": False,
            })
            first = False

    return S.table(
        "Lane fit by task class", cols, head, out,
        actions=[S.btn("Lanes & cost", S.page_link("lanes"), "ghost")],
        full=True, note=_LANE_NOTE,
    )


def _pass_rate_colour(rate_pct: float) -> str:
    if rate_pct >= 70:
        return "green"
    if rate_pct < 40:
        return "red"
    return "yellow"


# ── 3. Memories to review ───────────────────────────────────────────────


def _memories_to_review(home: Path) -> list[dict[str, Any]]:
    """A kv line + a table. Returned together so a single S.guarded keeps them paired."""
    kv_sec = _memory_counts_kv(home)
    table_sec = _memory_review_table(home)
    return [kv_sec, table_sec]


def _memory_counts_kv(home: Path) -> dict[str, Any]:
    conn = db(home)
    active = retired = 0
    if conn.has_table("semantic_memory"):
        cols = {r["name"] for r in conn.rows("PRAGMA table_info(semantic_memory)")}
        retired_expr = "COALESCE(retired_at,0)" if "retired_at" in cols else "0"
        row = conn.rows(
            f"SELECT "
            f"SUM(CASE WHEN {retired_expr} = 0 THEN 1 ELSE 0 END) AS active, "
            f"SUM(CASE WHEN {retired_expr} != 0 THEN 1 ELSE 0 END) AS retired "
            f"FROM semantic_memory"
        )[0] if conn.rows("SELECT 1 FROM semantic_memory LIMIT 1") else {"active": 0, "retired": 0}
        active = int(row.get("active") or 0)
        retired = int(row.get("retired") or 0)
    baseline_text = _overall_baseline(conn)
    return S.kv(_MEMORY_KV_TITLE, [
        ("Active", f"{active:,}"),
        ("Retired", f"{retired:,}", "sub"),
        ("Baseline", baseline_text),
    ], note=_MEMORY_NOTE)


def _overall_baseline(conn) -> str:
    if not conn.has_table("semantic_memory_uses"):
        return "—"
    row = conn.rows(
        "SELECT "
        "SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS wins, "
        "SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) AS losses "
        "FROM semantic_memory_uses"
    )
    if not row:
        return "—"
    wins = int(row[0].get("wins") or 0)
    losses = int(row[0].get("losses") or 0)
    if wins + losses <= 0:
        return "—"
    return f"{100.0 * wins / (wins + losses):.0f}%"


def _memory_review_table(home: Path) -> dict[str, Any]:
    from mini_ork.memory import candidates, RETIRE_MIN_USES

    conn = db(home)
    db_path = str(home / "state.db")
    cols_def = [S.col(fr=1, min=140), S.col(80), S.col(80), S.col(80), S.col(70), S.col(60), S.col(80)]
    head = ["memory", "scope", "win rate", "baseline", "Δ", "n", "state"]

    # Empty-state early-out — keeps the "Nothing to review" line crisp.
    if not conn.has_table("semantic_memory_uses"):
        return S.table(
            "Memories to review", cols_def, head,
            [[S.muted("Nothing to review — no memory is doing worse than its baseline"), "", "", "", "", "", ""]],
            full=True, note=_MEMORY_NOTE,
        )

    # Per-scope baselines from resolved uses (pending excluded).
    scope_rows = conn.rows(
        "SELECT scope, "
        "SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS wins, "
        "SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) AS losses "
        "FROM semantic_memory_uses GROUP BY scope"
    )
    baselines: dict[str, float] = {}
    for r in scope_rows:
        wins = int(r.get("wins") or 0)
        losses = int(r.get("losses") or 0)
        if wins + losses <= 0:
            continue
        baselines[str(r.get("scope") or "")] = 100.0 * wins / (wins + losses)

    # Per-memory resolved uses; only memories with n >= RETIRE_MIN_USES are ranked
    # against the baseline (others have too little evidence to retire).
    mem_rows = conn.rows(
        "SELECT memory_id, scope, "
        "SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS wins, "
        "SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) AS losses, "
        "COUNT(*) AS n "
        "FROM semantic_memory_uses "
        "GROUP BY memory_id, scope "
        f"HAVING n >= {int(RETIRE_MIN_USES)} "
        "AND SUM(CASE WHEN outcome='pending' THEN 1 ELSE 0 END) = 0"
    )
    flagged: dict[int, dict[str, Any]] = {}
    for r in mem_rows:
        scope = str(r.get("scope") or "")
        wins = int(r.get("wins") or 0)
        losses = int(r.get("losses") or 0)
        n = int(r.get("n") or 0)
        if wins + losses <= 0:
            continue
        rate = 100.0 * wins / (wins + losses)
        baseline = baselines.get(scope)
        if baseline is None:
            continue
        delta = rate - baseline
        if delta <= -5.0:
            flagged[int(r["memory_id"])] = {
                "memory_id": int(r["memory_id"]),
                "scope": scope,
                "rate": rate,
                "baseline": baseline,
                "delta": delta,
                "n": n,
                "source": "delta",
            }

    # Plus every candidate from memory.candidates(), per scope, scoped to scopes
    # that actually exist in semantic_memory.
    scopes = []
    if conn.has_table("semantic_memory"):
        scopes = [str(r.get("scope") or "")
                  for r in conn.rows("SELECT DISTINCT scope FROM semantic_memory")]
    for scope in scopes:
        try:
            for c in candidates(scope, db_path=db_path):
                cid = int(c.get("memory_id") or 0)
                if cid in flagged:
                    continue
                # candidates() reports the raw utility; mirror against baseline.
                base = baselines.get(scope)
                if base is None:
                    continue
                flagged[cid] = {
                    "memory_id": cid,
                    "scope": scope,
                    "rate": 100.0 * float(c.get("utility") or 0.0),
                    "baseline": base,
                    "delta": 100.0 * float(c.get("utility") or 0.0) - base,
                    "n": int(c.get("uses") or 0),
                    "source": "candidate",
                }
        except Exception:  # noqa: BLE001 — candidate fetch failure: still show what we have
            continue

    # Pull memory text + state for the flagged rows.
    if not flagged:
        return S.table(
            "Memories to review", cols_def, head,
            [[S.muted("Nothing to review — no memory is doing worse than its baseline"), "", "", "", "", "", ""]],
            full=True, note=_MEMORY_NOTE,
        )

    mem_ids = sorted(flagged.keys())
    placeholders = ",".join("?" for _ in mem_ids)
    mem_text = {}
    if conn.has_table("semantic_memory"):
        for r in conn.rows(
            f"SELECT id, text, retired_at FROM semantic_memory WHERE id IN ({placeholders})",
            mem_ids,
        ):
            mem_text[int(r["id"])] = r

    rows_out: list[Any] = []
    for cid, info in sorted(flagged.items(), key=lambda kv: kv[1]["delta"]):
        meta = mem_text.get(cid) or {}
        text_str = short(str(meta.get("text") or ""), 140)
        retired = bool(meta.get("retired_at"))
        state_label = "retired" if retired else "active"
        state_color = "sub" if retired else "text"
        if retired:
            action = S.cli("memory-lifecycle", "--reactivate", str(cid))
        else:
            action = S.cli(
                "memory-lifecycle", "--retire", str(cid), "--reason", _RETIRE_REASON,
                confirm=("Retire this memory? It stops being injected; "
                         "reactivate any time."),
            )
        rows_out.append({
            "cells": [
                S.cell(text_str, "text"),
                S.muted(info["scope"]),
                S.cell(f"{info['rate']:.0f}%", _pass_rate_colour(info["rate"])),
                S.muted(f"{info['baseline']:.0f}%"),
                S.cell(f"{info['delta']:+.0f}pt", "red"),
                S.mono(str(info["n"])),
                S.cell(state_label, state_color),
            ],
            "do": action,
        })

    return S.table(
        "Memories to review", cols_def, head, rows_out,
        full=True, note=_MEMORY_NOTE,
    )