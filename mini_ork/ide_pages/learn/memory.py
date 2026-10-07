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
from datetime import datetime, timedelta, timezone
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

_LANE_NOTE = ("From execution traces of the last 28 days (valid runs only: infra exits excluded). "
              "★ = best pass rate with at least 5 runs.")

_MEMORY_KV_TITLE = "Memories to review · counts"
_MEMORY_NOTE = ("Win = the run it was used in succeeded. Compared with the scope baseline, "
                "because most runs succeed either way. True per-lesson lift comes with the 10% holdout.")

_RETIRE_REASON = "operator request from Memory tab"


def sections(home: Path, args: dict[str, str], errors: dict[str, str],  # noqa: ARG001
             *, now: int | None = None) -> list[dict[str, Any]]:
    """Compose the three Memory tab sections in the kickoff's order.

    ``args`` is unused today (no per-page filter yet on the Memory tab); it stays
    in the signature so the dispatcher in ``learn/__init__.py`` calls every
    tab with the same shape. ``now`` is threaded through to the preferences
    section so tests can freeze the 7-day window without freezegun, the way
    ``overview.sections`` does.
    """
    del args
    moment = int(now) if now is not None else int(time.time())
    return (S.guarded(errors, _PREF_SECTION_TITLE, lambda: _preferences(home, moment))
            + S.guarded(errors, "Lane fit by task class", lambda: _lane_fit(home))
            + S.guarded(errors, "Memories to review", lambda: _memories_to_review(home)))


# ── 1. Preferences & constraints ─────────────────────────────────────────


def _preferences(home: Path, now: int) -> dict[str, Any]:
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
        sub_parts.append(_injection_sub(scope, target, str(p.get("key") or ""), p, db_path, now))
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


def _injection_sub(scope: str, target: str, key: str, pref: dict, db_path: str, now: int) -> str:
    """The "given to N node(s) in 7 days" / "not given ..." sub-fragment."""
    src = str(pref.get("source") or "")
    if not src.startswith("db"):
        # File-sourced prefs never reach the injection ledger.
        return "not given to any node in 7 days"
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
    if not conn.has_table("execution_traces"):
        return S.table(
            "Lane fit by task class",
            [S.col(140), S.col(110), S.col(180), S.col(60), S.col(80), S.col(80)],
            ["class", "role", "lane", "runs", "pass rate", "cost / run"],
            [[S.muted("No lane history yet — it fills as runs finish"), "", "", "", "", ""]],
            actions=[S.btn("Lanes & cost", S.page_link("lanes"), "ghost")],
            full=True, note=_LANE_NOTE,
        )

    # 28-day window, lexicographically comparable to the created_at shape
    # ``%Y-%m-%dT%H:%M:%fZ`` (see dispatch/calibration.recent_cutoff).
    cutoff = (datetime.now(timezone.utc) - timedelta(days=28)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = conn.rows(
        "WITH lanes AS ("
        "  SELECT task_class,"
        "    CASE"
        "      WHEN json_valid(verifier_output)"
        "           AND COALESCE(json_extract(verifier_output, '$.node_type'), '') <> ''"
        "        THEN json_extract(verifier_output, '$.node_type')"
        "      WHEN trace_id LIKE 'tr-%-%'"
        "        THEN substr(trace_id, 4, instr(substr(trace_id, 4), '-') - 1)"
        "      ELSE '?'"
        "    END AS role,"
        "    agent_version_id AS lane,"
        "    status, cost_usd"
        "  FROM execution_traces"
        "  WHERE agent_version_id <> ''"
        "    AND task_class <> ''"
        "    AND COALESCE(validity, 'valid') = 'valid'"
        "    AND status IN ('success', 'failure')"
        "    AND created_at >= ?"
        ") "
        "SELECT task_class, role, lane,"
        "  COUNT(*) AS runs,"
        "  SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success,"
        "  AVG(cost_usd) AS avg_cost_usd"
        " FROM lanes"
        " GROUP BY task_class, role, lane"
        " HAVING COUNT(*) >= 3",
        (cutoff,),
    )
    by_class: dict[str, list[dict]] = {}
    for r in rows:
        by_class.setdefault(str(r.get("task_class") or "unknown"), []).append(r)

    ranked = sorted(by_class.items(),
                    key=lambda kv: -sum(int(x.get("runs") or 0) for x in kv[1]))[:8]
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
            key=lambda r: (-(int(r.get("success") or 0)
                            / max(1, int(r.get("runs") or 0))),
                           -int(r.get("runs") or 0)),
        )[:4]
        # Best lane for the class — highest pass rate with runs >= 5.
        eligible = [r for r in lanes_sorted if int(r.get("runs") or 0) >= 5]
        if eligible:
            best = max(
                eligible,
                key=lambda r: int(r.get("success") or 0)
                              / max(1, int(r.get("runs") or 0)),
            )
            best_key = (best.get("role"), best.get("lane"))
        else:
            best_key = None
        first = True
        for r in lanes_sorted:
            runs = int(r.get("runs") or 0)
            succ = int(r.get("success") or 0)
            rate = (100.0 * succ / runs) if runs else 0.0
            rate_text = f"{rate:.0f}%"
            rate_color = _pass_rate_colour(rate)
            cost = float(r.get("avg_cost_usd") or 0.0)
            lane = str(r.get("lane") or "?")
            if (r.get("role"), r.get("lane")) == best_key:
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


def _delta_colour(delta: float | None) -> str:
    """Δ cell colour: red ≤ −5 pt, green ≥ +5 pt, muted otherwise (or no data)."""
    if delta is None:
        return "muted"
    if delta <= -5.0:
        return "red"
    if delta >= 5.0:
        return "green"
    return "muted"


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


def _resolved(conn, memory_id: int, scope: str) -> tuple[int, int]:
    """Resolved ``(wins, losses)`` for one (memory, scope) from the use ledger.

    ``pending`` rows are unresolved and must not feed the win rate, ``n``, or
    any baseline comparison — only ``win`` and ``loss`` count.
    """
    if not conn.has_table("semantic_memory_uses"):
        return 0, 0
    rows = conn.rows(
        "SELECT "
        "SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS wins, "
        "SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) AS losses "
        "FROM semantic_memory_uses WHERE memory_id = ? AND scope = ?",
        (memory_id, scope),
    )
    if not rows:
        return 0, 0
    return int(rows[0].get("wins") or 0), int(rows[0].get("losses") or 0)


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

    # Per-memory resolved uses; only memories with wins + losses >= RETIRE_MIN_USES
    # are ranked against the baseline (others have too little evidence to retire).
    # ``n`` is resolved uses only — pending must not count, so a single pending
    # row no longer drops an otherwise-qualifying group.
    mem_rows = conn.rows(
        "SELECT memory_id, scope, "
        "SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS wins, "
        "SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) AS losses, "
        "SUM(CASE WHEN outcome IN ('win', 'loss') THEN 1 ELSE 0 END) AS n "
        "FROM semantic_memory_uses "
        "GROUP BY memory_id, scope "
        f"HAVING SUM(CASE WHEN outcome IN ('win', 'loss') THEN 1 ELSE 0 END) >= {int(RETIRE_MIN_USES)}"
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
        # Round once so the flag threshold, the displayed Δ text and its colour
        # all read the same value (round() also normalises −0.x to 0 → "+0pt").
        delta_pt = round(delta)
        if delta_pt <= -5:
            flagged[int(r["memory_id"])] = {
                "memory_id": int(r["memory_id"]),
                "scope": scope,
                "rate": rate,
                "baseline": baseline,
                "delta": delta_pt,
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
                # candidates() reports smoothed utility from the denormalized
                # semantic_memory counters — NOT the resolved win rate the
                # baseline uses. Re-derive the rate from the use ledger so the
                # two columns stay commensurable.
                base = baselines.get(scope)
                if base is None:
                    continue
                wins, losses = _resolved(conn, cid, scope)
                n = wins + losses
                if n > 0:
                    rate = 100.0 * wins / n
                    # Same rounding as the delta path so text, colour and the
                    # listing agree (round() normalises −0.x to 0 → "+0pt").
                    delta_pt = round(rate - base)
                else:
                    rate = None
                    delta_pt = None
                flagged[cid] = {
                    "memory_id": cid,
                    "scope": scope,
                    "rate": rate,
                    "baseline": base,
                    "delta": delta_pt,
                    "n": n,
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
    for cid, info in sorted(
        flagged.items(),
        key=lambda kv: kv[1]["delta"] if kv[1]["delta"] is not None else 1e9,
    ):
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
        rate_cell = (S.cell(f"{info['rate']:.0f}%", _pass_rate_colour(info["rate"]))
                     if info["rate"] is not None else S.muted("—"))
        delta_text = f"{info['delta']:+.0f}pt" if info["delta"] is not None else "—"
        rows_out.append({
            "cells": [
                S.cell(text_str, "text"),
                S.muted(info["scope"]),
                rate_cell,
                S.muted(f"{info['baseline']:.0f}%"),
                S.cell(delta_text, _delta_colour(info["delta"])),
                S.mono(str(info["n"])),
                S.cell(state_label, state_color),
            ],
            "do": action,
        })

    return S.table(
        "Memories to review", cols_def, head, rows_out,
        full=True, note=_MEMORY_NOTE,
    )