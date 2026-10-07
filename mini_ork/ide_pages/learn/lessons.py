"""Lessons tab — Gradients, Patterns and Failure modes.

Every row opens: selecting a row sets ``args["item"]`` and a full-text detail
panel renders first. The page is strictly read-only — the IDE's ``StateDB`` is
opened ``query_only``, so every statement here is a SELECT or a file read, and
every query tolerates a missing table or column (a bare home degrades to an
empty state rather than a crash).

Item encoding: ``g:<gradient_id>``, ``p:<pattern_id>``, ``f:<category>|<stage>``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S
from mini_ork.ide_pages.learn._common import count, db, short

_GRADIENT_PAGE = 25   # gradients per page
_PATTERN_ROWS = 20    # patterns shown
_SIMILAR_ROWS = 8     # other theme members listed on a gradient
_FAILURE_ERRORS = 5   # full error messages shown on a failure-mode detail
_MISSING = "That learning no longer exists"


def sections(home: Path, args: dict[str, str], errors: dict[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    item = str(args.get("item") or "").strip()
    if item:
        out += S.guarded(errors, "Learning", lambda: _detail(home, item))
    out += S.guarded(errors, "Gradients", lambda: _gradients(home, args))
    out += S.guarded(errors, "Patterns", lambda: _patterns(home, args))
    out += S.guarded(errors, "Failure modes", lambda: _failure_modes(home, args))
    return out


# ── detail dispatch ─────────────────────────────────────────────────────────

def _missing() -> list[dict[str, Any]]:
    return [S.lst("Learning", [S.dot(_MISSING)],
                  actions=[S.btn("Close", S.set_args(item=""), "ghost")], full=True)]


def _detail(home: Path, item: str) -> list[dict[str, Any]]:
    kind, _, rest = item.partition(":")
    if kind == "g" and rest:
        return _gradient_detail(home, rest)
    if kind == "p" and rest:
        return _pattern_detail(home, rest)
    if kind == "f" and "|" in rest:
        category, _, stage = rest.partition("|")
        return _failure_detail(home, category, stage)
    return _missing()


def _gradient_detail(home: Path, gradient_id: str) -> list[dict[str, Any]]:
    conn = db(home)
    row = conn.row(
        "SELECT gradient_id, target, signal, suggested_change, evidence, "
        "confidence, task_class, created_at FROM gradient_records WHERE gradient_id = ?",
        (gradient_id,)) if conn.has_table("gradient_records") else None
    if not row:
        return _missing()
    target = str(row.get("target") or "gradient")
    signal = str(row.get("signal") or "")
    change = str(row.get("suggested_change") or "")
    body = f"**What was observed**\n\n{signal}\n\n**What to change**\n\n{change}"
    title = f"Learning · {target}"
    out: list[dict[str, Any]] = [S.markdown(title, body, full=True)]

    run_id, run_title = _run_of_trace(home, conn, str(row.get("evidence") or ""))
    actions: list[dict[str, Any]] = []
    if run_id:
        actions.append(S.btn("Open run", S.open_run(run_id, run_title), "primary"))
    actions.append(S.btn("Close", S.set_args(item=""), "ghost"))

    theme = _theme_of_gradient(conn, gradient_id)
    items: list[tuple] = [
        ("Confidence", f"{float(row.get('confidence') or 0):.2f}"),
        ("Task class", str(row.get("task_class") or "any")),
        ("Recorded", _epoch_date(row.get("created_at"))),
        ("Evidence", run_title or "—", "text", run_id or "no run trace recorded"),
    ]
    if theme:
        items.append(("Theme", short(theme["representative"], 160), "text", theme["sub"]))
    else:
        items.append(("Theme", "—", "sub", "not grouped into a theme yet"))
    items.append(("Given to agents", _injection_text(home, conn, "gradient", gradient_id)))
    out.append(S.kv("", items, actions=actions, full=True))

    similar = _similar_notes(conn, theme["theme_id"] if theme else "", gradient_id)
    if similar is not None:
        out.append(similar)
    return out


def _pattern_detail(home: Path, pattern_id: str) -> list[dict[str, Any]]:
    conn = db(home)
    if not conn.has_table("emergent_patterns"):
        return _missing()
    has_lesson = _has_column(conn, "emergent_patterns", "lesson_text")
    cols = ("pattern_id, cluster_label, strength_score, status, member_item_ids_json"
            + (", lesson_text" if has_lesson else ""))
    row = conn.row(f"SELECT {cols} FROM emergent_patterns WHERE pattern_id = ?",  # noqa: S608 — fixed cols
                   (pattern_id,))
    if not row:
        return _missing()
    label = str(row.get("cluster_label") or "")
    lesson = (str(row.get("lesson_text") or "") if has_lesson else "")
    lesson_block = lesson or "No lesson authored yet — this pattern is only a frequency count."
    body = f"**Lesson**\n\n{lesson_block}\n\n**Cluster**\n\n{label}"
    items = [
        ("Status", str(row.get("status") or "—")),
        ("Strength", f"{float(row.get('strength_score') or 0):.2f}"),
        ("Evidence runs", str(_independent_runs(conn, row.get("member_item_ids_json")))),
        ("Given to agents", _injection_text(home, conn, "pattern", pattern_id)),
    ]
    actions = [S.btn("Close", S.set_args(item=""), "ghost")]
    return [S.markdown(f"Pattern · {pattern_id}", body, full=True),
            S.kv("", items, actions=actions, full=True)]


def _failure_detail(home: Path, category: str, stage: str) -> list[dict[str, Any]]:
    conn = db(home)
    if not conn.has_table("failure_memory"):
        return _missing()
    rows = conn.rows(
        "SELECT failure_id, run_id, error_message, occurred_at FROM failure_memory "
        "WHERE COALESCE(failure_category, 'unknown') = ? AND COALESCE(workflow_stage, 'any stage') = ? "
        "ORDER BY occurred_at DESC LIMIT ?", (category, stage, _FAILURE_ERRORS))
    if not rows:
        return _missing()
    blocks: list[str] = []
    actions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        rid, title = _run_title(home, r.get("run_id"))
        date = str(r.get("occurred_at") or "")[:10]
        heading = f"{title} · {date}" if title else f"run {r.get('run_id')} · {date}"
        msg = str(r.get("error_message") or "")
        block = f"```\n{msg}\n```" if "\n" in msg else msg
        blocks.append(f"**{heading}**\n\n{block}")
        if rid and rid not in seen:
            seen.add(rid)
            actions.append(S.btn("Open run", S.open_run(rid, title)))
    agg_rows = conn.rows(
        "SELECT COUNT(*) AS n, COUNT(DISTINCT run_id) AS runs, MAX(occurred_at) AS last "
        "FROM failure_memory WHERE COALESCE(failure_category, 'unknown') = ? AND COALESCE(workflow_stage, 'any stage') = ?", (category, stage))
    agg = agg_rows[0] if agg_rows else {}
    recovery = _recovery_for(conn, category)
    items = [
        ("Occurrences", str(int(agg.get("n") or 0))),
        ("Runs", str(int(agg.get("runs") or 0))),
        ("Last seen", str(agg.get("last") or "—")[:10]),
        ("Recovery that worked", recovery or "—", "green" if recovery else "sub"),
    ]
    actions.append(S.btn("Close", S.set_args(item=""), "ghost"))
    return [S.markdown(f"Failure mode · {category} · {stage}", "\n\n".join(blocks), full=True),
            S.kv("", items, actions=actions, full=True)]


# ── list sections ───────────────────────────────────────────────────────────

def _gradients(home: Path, args: dict[str, str]) -> dict[str, Any]:
    conn = db(home)
    item = str(args.get("item") or "")
    total = count(conn, "gradient_records")
    try:
        off = max(0, int(args.get("goff") or 0))
    except (TypeError, ValueError):
        off = 0
    rows = conn.rows(
        "SELECT gradient_id, target, signal, confidence, task_class FROM gradient_records "
        "ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
        (_GRADIENT_PAGE, off)) if conn.has_table("gradient_records") else []
    ids = [str(r.get("gradient_id")) for r in rows]
    counts = _injection_counts(home, conn, "gradient", ids)
    out: list[Any] = []
    for r in rows:
        gid = str(r.get("gradient_id"))
        uses = int((counts.get(gid) or {}).get("uses") or 0)
        out.append({"cells": [
            S.mono(r.get("target") or "gradient"),
            S.cell(short(r.get("signal") or "", 160)),
            S.muted(r.get("task_class") or "any"),
            S.mono(f"{float(r.get('confidence') or 0):.2f}"),
            S.mono(str(uses)) if uses else S.muted("—"),
        ], "do": S.set_args(item=f"g:{gid}"), "sel": item == f"g:{gid}"})
    if not out:
        out = [[S.muted("No gradients yet — reflection writes them as runs finish"), "", "", "", ""]]
    actions: list[dict[str, Any]] = []
    if off > 0:
        actions.append(S.btn("Newer", S.set_args(goff=max(0, off - _GRADIENT_PAGE))))
    if off + _GRADIENT_PAGE < total:
        actions.append(S.btn("Older", S.set_args(goff=off + _GRADIENT_PAGE)))
    shown = f"showing {off + 1}–{off + len(rows)} of {total:,}" if rows else f"showing 0 of {total:,}"
    return S.table("Gradients", [S.col(140), S.col(fr=1, min=160), S.col(90), S.col(44), S.col(64)],
                   ["target", "signal", "task class", "conf", "injected"], out, full=True,
                   actions=actions,
                   note=f"Reflection turns traces into natural-language gradients · {shown}, newest "
                        "first. Click a row to read it in full; high-confidence ones are injected "
                        "into node prompts at dispatch.")


def _patterns(home: Path, args: dict[str, str]) -> dict[str, Any]:
    conn = db(home)
    item = str(args.get("item") or "")
    if not conn.has_table("emergent_patterns"):
        return S.lst("Patterns", [S.dot("No patterns yet")])
    has_lesson = _has_column(conn, "emergent_patterns", "lesson_text")
    cols = ("pattern_id, cluster_label, strength_score, status" + (", lesson_text" if has_lesson else ""))
    try:
        rows = conn.rows(f"SELECT {cols} FROM emergent_patterns "  # noqa: S608 — fixed cols
                         "ORDER BY strength_score DESC, detected_at DESC LIMIT ?", (_PATTERN_ROWS,))
    except Exception:  # noqa: BLE001 — a legacy schema must not blank the section
        rows = []
    if not rows:
        return S.lst("Patterns", [S.dot("No patterns yet")])
    out: list[Any] = []
    for r in rows:
        pid = str(r.get("pattern_id") or "")
        label = str(r.get("cluster_label") or pid)
        lesson = str(r.get("lesson_text") or "") if has_lesson else ""
        heading = short(lesson, 200) if lesson else f"{short(label, 160)} (no lesson)"
        out.append({"cells": [
            S.cell(heading),
            S.mono(f"{float(r.get('strength_score') or 0):.2f}"),
            S.muted(str(r.get("status") or "proposed")),
        ], "do": S.set_args(item=f"p:{pid}"), "sel": item == f"p:{pid}"})
    return S.table("Patterns", [S.col(fr=1, min=200), S.col(70), S.col(90)],
                   ["pattern", "strength", "status"], out, full=True,
                   note=f"Strongest first ({count(conn, 'emergent_patterns'):,} detected). The row "
                        "title is the authored lesson when one exists, else the cluster label.")


def _failure_modes(home: Path, args: dict[str, str]) -> dict[str, Any]:
    """failure_memory grouped by category and stage, with the recovery that worked if one is recorded."""
    conn = db(home)
    item = str(args.get("item") or "")
    rows = conn.rows(
        "SELECT failure_category AS category, workflow_stage AS stage, COUNT(*) AS n, "
        "COUNT(DISTINCT run_id) AS runs, MAX(occurred_at) AS last "
        "FROM failure_memory GROUP BY failure_category, workflow_stage ORDER BY n DESC LIMIT 6") \
        if conn.has_table("failure_memory") else []
    if not rows:
        return S.lst("Failure modes", [S.ok("No failure modes recorded")])
    recoveries: dict[str, str] = {}
    if conn.has_table("recovery_memory"):
        for r in conn.rows("SELECT f.failure_category AS category, m.recovery_action AS action "
                           "FROM recovery_memory m JOIN failure_memory f ON f.failure_id = m.failure_id "
                           "WHERE m.outcome = 'success' ORDER BY m.recovered_at DESC"):
            recoveries.setdefault(str(r.get("category")), str(r.get("action") or ""))
    out: list[Any] = []
    for r in rows:
        n, runs = int(r.get("n") or 0), int(r.get("runs") or 0)
        cat = str(r.get("category") or "unknown")
        stage = str(r.get("stage") or "any stage")
        rec = recoveries.get(cat)
        out.append({"cells": [
            S.cell(f"{cat} · {stage}", "red" if n >= 3 else "yellow"),
            S.mono(str(n)),
            S.mono(str(runs)),
            S.muted(str(r.get("last") or "—")[:10]),
            S.muted(short(rec, 60) if rec else "—"),
        ], "do": S.set_args(item=f"f:{cat}|{stage}"), "sel": item == f"f:{cat}|{stage}"})
    return S.table("Failure modes", [S.col(fr=1, min=180), S.col(60), S.col(56), S.col(96),
                                     S.col(fr=1, min=140)],
                   ["failure mode", "count", "runs", "last", "recovery"], out, full=True,
                   note="Grouped by category and stage, most frequent first. Click a row to read "
                        "the full error messages and open the runs behind them.")


# ── read-only helpers ───────────────────────────────────────────────────────

def _epoch_date(epoch: Any) -> str:
    """Epoch seconds → ``YYYY-MM-DD HH:MM`` (UTC); ``—`` when not a timestamp."""
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "—"


def _has_column(conn, table: str, column: str) -> bool:
    try:
        return any(str(r.get("name")) == column for r in conn.rows(f"PRAGMA table_info({table})"))
    except Exception:  # noqa: BLE001 — a probe must never blank the section
        return False


def _injection_counts(home: Path, conn, kind: str, ids: list[str]) -> dict[str, dict]:
    """Real injection counts, or ``{}`` when the ledger table is absent.

    The DB is passed explicitly (``db=str(home / "state.db")``): the ledger's own
    default walks ``MINI_ORK_DB``/``MINI_ORK_HOME`` and would otherwise read a
    different home's DB — a silent wrong-home read that shows "not given to any
    agent yet" for lessons that were injected.
    """
    if not ids or not conn.has_table("lesson_injections"):
        return {}
    from mini_ork.learning.ledger import injection_counts
    return injection_counts(kind, ids, db=str(home / "state.db"))


def _injection_text(home: Path, conn, kind: str, source_id: str) -> str:
    entry = (_injection_counts(home, conn, kind, [source_id]).get(str(source_id)) or {})
    uses = int(entry.get("uses") or 0)
    if not uses:
        return "not given to any agent yet"
    return f"{uses}× · last {_epoch_date(entry.get('last_ts'))}"


def _run_title(home: Path, run_id: Any) -> tuple[str, str]:
    """``(run_id, title)`` from ``task_runs``; ``("", "")`` when the id does not resolve.

    A run id that is not a live ``task_runs`` row (the common case for legacy
    ``failure_memory.run_id`` integers) is reported as missing rather than
    rendered as a broken "Open run" link.
    """
    rid = str(run_id or "").strip()
    if not rid:
        return "", ""
    from mini_ork.acp.history import runs_by_ids
    rows = runs_by_ids(home, [rid])
    if not rows:
        return "", ""
    return str(rows[0].get("run_id") or rid), str(rows[0].get("title") or rid)


def _run_of_trace(home: Path, conn, trace_id: str) -> tuple[str, str]:
    """``(run_id, title)`` behind a gradient's evidence trace id; ``("", "")`` if unknown."""
    if not trace_id or not conn.has_table("execution_traces"):
        return "", ""
    row = conn.row("SELECT run_id FROM execution_traces WHERE trace_id = ? LIMIT 1", (trace_id,))
    return _run_title(home, row.get("run_id") if row else "")


def _theme_of_gradient(conn, gradient_id: str) -> dict[str, str] | None:
    if not (conn.has_table("gradient_theme") and conn.has_table("lesson_themes")):
        return None
    row = conn.row(
        "SELECT t.theme_id, t.representative, t.n_gradients, t.n_runs, t.kind "
        "FROM gradient_theme gt JOIN lesson_themes t ON t.theme_id = gt.theme_id "
        "WHERE gt.gradient_id = ? LIMIT 1", (gradient_id,))
    if not row:
        return None
    label = "about mini-ork itself" if str(row.get("kind")) == "framework" else "about the task"
    sub = (f"{int(row.get('n_gradients') or 0)} similar notes across "
           f"{int(row.get('n_runs') or 0)} runs · {label}")
    return {"theme_id": str(row.get("theme_id") or ""),
            "representative": str(row.get("representative") or ""), "sub": sub}


def _similar_notes(conn, theme_id: str, gradient_id: str) -> dict[str, Any] | None:
    if not theme_id or not (conn.has_table("gradient_theme") and conn.has_table("gradient_records")):
        return None
    rows = conn.rows(
        "SELECT g.gradient_id, g.signal FROM gradient_theme gt "
        "JOIN gradient_records g ON g.gradient_id = gt.gradient_id "
        "WHERE gt.theme_id = ? AND gt.gradient_id != ? "
        "ORDER BY g.created_at DESC, g.rowid DESC LIMIT ?",
        (theme_id, gradient_id, _SIMILAR_ROWS))
    if not rows:
        return None
    out = [{"cells": [S.cell(short(r.get("signal") or "", 140))],
            "do": S.set_args(item=f"g:{r.get('gradient_id')}"), "sel": False} for r in rows]
    return S.table("Similar notes in this theme", [S.col(fr=1, min=200)], ["signal"], out, full=True)


def _member_trace_ids(members_json: Any) -> list[str]:
    """Trace ids named by an emergent_pattern's member list (mirrors the miner's rule)."""
    try:
        members = json.loads(members_json) if members_json else []
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(members, list):
        return []
    ids: list[str] = []
    for m in members:
        if isinstance(m, dict):
            if (m.get("item_table") or "execution_traces") != "execution_traces":
                continue
            mid = m.get("item_id")
        else:
            mid = m
        if mid:
            ids.append(str(mid))
    return ids


def _independent_runs(conn, members_json: Any) -> int:
    """Distinct runs behind a pattern's member traces — fail-closed on every gap."""
    ids = _member_trace_ids(members_json)
    if not ids:
        return 0
    if not conn.has_table("execution_traces"):
        return len(set(ids))
    try:
        placeholders = ",".join("?" for _ in ids)
        row = conn.row(
            f"SELECT COUNT(DISTINCT NULLIF(TRIM(COALESCE(run_id,'')),'')) AS n "  # noqa: S608 — placeholders
            f"FROM execution_traces WHERE trace_id IN ({placeholders})", tuple(ids))
        return int(row.get("n") or 0) if row else 0
    except Exception:  # noqa: BLE001 — unreadable traces → distinct-id fallback
        return len(set(ids))


def _recovery_for(conn, category: str) -> str:
    """The recovery action recorded for ``category`` (existing success-outcome rule)."""
    if not (conn.has_table("recovery_memory") and conn.has_table("failure_memory")):
        return ""
    try:
        rows = conn.rows(
            "SELECT m.recovery_action AS action FROM recovery_memory m "
            "JOIN failure_memory f ON f.failure_id = m.failure_id "
            "WHERE m.outcome = 'success' AND f.failure_category = ? "
            "ORDER BY m.recovered_at DESC LIMIT 1", (category,))
    except Exception:  # noqa: BLE001 — recovery is a bonus fact, not a hard dependency
        return ""
    return str(rows[0].get("action") or "") if rows else ""
