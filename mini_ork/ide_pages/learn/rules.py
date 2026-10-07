"""Rules tab — everything agents are told in this repo, and control over it.

Answers one engineer question: *what will the agents follow when they work on
my code, and how do I change that?* from the two sources that actually reach a
node prompt:

- **Your rules** — ``user_preference_memory`` (scoped global / task_class /
  workflow / path) plus the legacy config files, injected FIRST in every
  researcher / implementer / reviewer prompt (``preferences.prefs_for``).
- **Learned rules (verified)** — ``emergent_patterns`` rows with
  ``status='approved'`` and a non-blank ``lesson_text``, injected under
  "Lessons from recurring patterns" (``context_assembler``).

The page is strictly read-only: the state DB is opened ``query_only``, every
query tolerates a missing table/column, and every mutation is an ``S.cli``
action (``prefs rm`` / ``lessons forget`` / ``prefs set``) the IDE runs — the
page itself never writes. Selecting a rule (``args["rule"]``) renders its full
detail first; selecting a run + node (``args["preview"]`` / ``args["node"]``)
renders the exact block that node would receive.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from mini_ork.context import scoped_environ
from mini_ork.ide_pages import spec as S
from mini_ork.ide_pages.learn._common import db, short

# The ledger helpers the Lessons tab already owns — reused rather than copied,
# so a lesson is counted the same way on both tabs.
from mini_ork.ide_pages.learn.lessons import (
    _has_column,
    _independent_runs,
    _injection_text,
)

_PREVIEW_RUNS = 8   # recent runs offered as preview chips
_NODES = ("implementer", "reviewer", "researcher")
_SEVEN_DAYS = 7 * 86400

_ADD_RULE_THREAD = (
    "I want to add a rule for mini-ork agents in this repo. Ask me what it is "
    "and where it applies (everywhere, a task type, or certain files), then run "
    "mini-ork prefs set."
)
_RULES_NOTE = ("Given first to every researcher, implementer and reviewer whose "
               "work it applies to. File rules apply only when the task touches "
               "those files.")
_RULES_EMPTY = ("No rules yet. Add one, or turn a recurring review problem into "
                "a rule from the Your code tab.")


def sections(home: Path, args: dict[str, str], errors: dict[str, str], *,
             now: int | None = None) -> list[dict[str, Any]]:
    moment = int(now if now is not None else time.time())
    out: list[dict[str, Any]] = []
    rule = str(args.get("rule") or "").strip()
    if rule:  # detail-first: a selected rule renders above the lists
        out += S.guarded(errors, "Rule", lambda: _rule_detail(home, rule))
    out += S.guarded(errors, "Your rules", lambda: _your_rules(home, moment))
    out += S.guarded(errors, "Learned rules (verified)", lambda: _learned_rules(home))
    out += S.guarded(errors, "What will an agent be told?",
                     lambda: _preview(home, args))
    return out


# ── Your rules ──────────────────────────────────────────────────────────────

def _safe_list_prefs(db_path: str) -> list[dict]:
    """Read prefs but never raise on a missing DB / file. Empty list on failure."""
    try:
        from mini_ork.memory.preferences import list_prefs
        return list_prefs(db=db_path)
    except Exception:  # noqa: BLE001 — a broken source must not blank the page
        return []


def _applies_to(scope: str, target: str) -> str:
    if scope == "global":
        return "everyone"
    if scope == "task_class":
        return f"task: {target}"
    if scope == "workflow":
        return f"workflow: {target}"
    if scope == "path":
        return f"files: {target}"
    return f"{scope}: {target}"


def _given_7d(scope: str, target: str, key: str, pref: dict, db_path: str, now: int) -> str:
    """Ledger injections in the last 7 days, or ``not yet``."""
    if not str(pref.get("source") or "").startswith("db"):
        return "not yet"   # file-sourced rules never reach the injection ledger
    source_id = f"pref:{scope}:{target}:{key}"
    try:
        from mini_ork.learning.ledger import injection_counts
        info = injection_counts("preference", [source_id], since=now - _SEVEN_DAYS,
                                db=db_path)
    except Exception:  # noqa: BLE001 — silent ledger failure → "not yet"
        return "not yet"
    uses = int((info.get(source_id) or {}).get("uses") or 0)
    return f"{uses}×" if uses > 0 else "not yet"


def _remove_action(key: str, scope: str, target: str, source: str) -> dict[str, Any]:
    if source.startswith("file:"):
        return S.open_path(source[len("file:"):])
    return S.cli("prefs", "rm", key, "--scope", scope, "--target", target,
                 confirm=f"Remove rule '{key}'?")


def _your_rules(home: Path, now: int) -> dict[str, Any]:
    db_path = str(home / "state.db")
    prefs = _safe_list_prefs(db_path)
    out: list[Any] = []
    for p in prefs:
        key = str(p.get("key") or "")
        scope = str(p.get("scope") or "global")
        target = str(p.get("target") or "")
        value = str(p.get("value") or "")
        src = str(p.get("source") or "")
        given = _given_7d(scope, target, key, p, db_path, now)
        out.append({"cells": [
            S.cell(short(value, 140)),
            S.muted(_applies_to(scope, target)),
            S.mono(given) if given != "not yet" else S.muted("not yet"),
            S.muted("file" if src.startswith("file:") else "you"),
        ], "do": _remove_action(key, scope, target, src), "sel": False})
    if not out:
        out = [[S.muted(_RULES_EMPTY), "", "", ""]]
    return S.table("Your rules",
                   [S.col(fr=1, min=200), S.col(120), S.col(150), S.col(70)],
                   ["rule", "applies to", "given to agents (7 days)", "source"],
                   out, full=True,
                   actions=[S.btn("Add a rule", S.thread(_ADD_RULE_THREAD), "primary")],
                   note=_RULES_NOTE)


# ── Learned rules (verified) ────────────────────────────────────────────────

def _waiting_count(conn) -> int:
    """Proposed rows that already carry a lesson — waiting for the judge gate."""
    try:
        row = conn.row(
            "SELECT COUNT(*) AS n FROM emergent_patterns "
            "WHERE status='proposed' AND COALESCE(lesson_text,'') != ''")
    except Exception:  # noqa: BLE001 — a count must never blank the section
        return 0
    return int(row.get("n") or 0) if row else 0


def _learned_note(conn) -> str:
    waiting = _waiting_count(conn)
    tail = (f"{waiting} more are waiting for verification."
            if waiting else "Nothing else is waiting for verification.")
    return ("Only verified lessons reach agents: seen across independent runs, "
            f"with an authored lesson. {tail}")


def _learned_rules(home: Path) -> dict[str, Any]:
    conn = db(home)
    if not (conn.has_table("emergent_patterns")
            and _has_column(conn, "emergent_patterns", "lesson_text")):
        return _learned_empty("No verified lessons yet.")
    # No LIMIT before the lesson filter: a cap would let lessonless approved
    # rows crowd out the ones that count (the "cap-then-aggregate" defect).
    rows = conn.rows(
        "SELECT pattern_id, cluster_label, member_item_ids_json, strength_score, lesson_text "
        "FROM emergent_patterns WHERE status='approved' "
        "ORDER BY strength_score DESC, detected_at DESC")
    verified = [r for r in rows if str(r.get("lesson_text") or "").strip()]
    if not verified:
        return _learned_empty("No verified lessons yet.", conn)
    out: list[Any] = []
    for r in verified:
        pid = str(r.get("pattern_id") or "")
        lesson = str(r.get("lesson_text") or "")
        seen = _independent_runs(conn, r.get("member_item_ids_json"))
        out.append({"cells": [
            S.cell(short(lesson, 140)),
            S.mono(str(seen)),
            S.muted(_injection_text(home, conn, "pattern", pid)),
        ], "do": S.set_args(rule=pid), "sel": False})
    return S.table("Learned rules (verified)",
                   [S.col(fr=1, min=240), S.col(70), S.col(180)],
                   ["lesson", "seen in", "given to agents"], out, full=True,
                   note=_learned_note(conn))


def _learned_empty(message: str, conn=None) -> dict[str, Any]:
    note = _learned_note(conn) if conn is not None else (
        "Only verified lessons reach agents: seen across independent runs, with "
        "an authored lesson.")
    return S.lst("Learned rules (verified)", [S.dot(message)], full=True, note=note)


# ── Rule detail (rendered first when args["rule"] is set) ────────────────────

def _rule_missing() -> list[dict[str, Any]]:
    return [S.lst("Rule", [S.dot("That rule no longer exists")],
                  actions=[S.btn("Close", S.set_args(rule=""), "ghost")], full=True)]


def _recent_giving_runs(home: Path, conn, pid: str) -> list[tuple[str, str]]:
    """The last up-to-5 runs the lesson was given in: ``[(run_id, title)]``."""
    if not conn.has_table("lesson_injections"):
        return []
    try:
        rows = conn.rows(
            "SELECT run_id, MAX(ts) AS t FROM lesson_injections "
            "WHERE source_kind='pattern' AND source_id=? AND COALESCE(held_out,0)=0 "
            "GROUP BY run_id ORDER BY t DESC LIMIT 5", (pid,))
    except Exception:  # noqa: BLE001 — a missing/legacy ledger → no run links
        return []
    ids = [str(r.get("run_id")) for r in rows if r.get("run_id")]
    if not ids:
        return []
    from mini_ork.acp.history import runs_by_ids
    meta = {str(m.get("run_id")): str(m.get("title") or "") for m in runs_by_ids(home, ids)}
    return [(rid, meta.get(rid) or rid) for rid in ids]


def _rule_detail(home: Path, rule: str) -> list[dict[str, Any]]:
    conn = db(home)
    if not conn.has_table("emergent_patterns"):
        return _rule_missing()
    has_lesson = _has_column(conn, "emergent_patterns", "lesson_text")
    cols = ("pattern_id, cluster_label, member_item_ids_json, strength_score, status"
            + (", lesson_text" if has_lesson else ""))
    row = conn.row(f"SELECT {cols} FROM emergent_patterns WHERE pattern_id = ?",  # noqa: S608 — fixed cols
                   (rule,))
    if not row:
        return _rule_missing()
    pid = str(row.get("pattern_id") or rule)
    lesson = str(row.get("lesson_text") or "").strip() if has_lesson else ""
    label = str(row.get("cluster_label") or "")
    seen = _independent_runs(conn, row.get("member_item_ids_json"))
    body = (
        f"**Lesson**\n\n{lesson or 'No lesson authored yet — this is a frequency count, not guidance.'}\n\n"
        f"**Cluster**\n\n{label}\n\n"
        f"Seen in {seen} independent run(s) · status {str(row.get('status') or 'proposed')}."
    )
    actions: list[dict[str, Any]] = []
    for rid, title in _recent_giving_runs(home, conn, pid):
        actions.append(S.btn(f"Open {short(title, 30)}", S.open_run(rid, title), "ghost"))
    actions.append(S.btn("Forget",
                         S.cli("lessons", "forget", pid,
                               confirm="Stop giving this lesson to agents?"),
                         "danger" if lesson else "ghost"))
    if lesson:
        actions.append(S.btn(
            "Make it mine",
            S.cli("prefs", "set", f"learned-{pid[:12]}", lesson, "--scope", "global",
                  confirm=f"Copy this lesson into your own rules as 'learned-{pid[:12]}'?"),
            "ghost"))
    actions.append(S.btn("Close", S.set_args(rule=""), "ghost"))
    return [S.markdown(f"Rule · {pid}", body, full=True, actions=actions)]


# ── What will an agent be told? (read-only preview) ──────────────────────────

def _recent_kickoff_runs(home: Path, conn) -> list[dict[str, str]]:
    """Up to 8 recent runs whose kickoff is readable — the preview chips.

    No LIMIT before the readability filter. The ``kickoff_path`` column can
    point at a file that is gone, and a cap applied to the raw row stream would
    let those unreadable runs crowd out the newest readable ones (the same
    cap-before-filter shape as the ``learn.code`` area query). Candidates are
    walked newest-first and the walk stops at ``_PREVIEW_RUNS`` readable runs;
    ``kickoff_text`` is the same reader ``_preview_block`` uses, so a chip is
    offered iff selecting it yields a non-empty preview block.
    """
    if not conn.has_table("task_runs"):
        return []
    try:
        rows = conn.rows(
            "SELECT id, task_class FROM task_runs "
            "WHERE COALESCE(kickoff_path,'') != '' "
            "ORDER BY created_at DESC")
    except Exception:  # noqa: BLE001 — a run list must never blank the section
        return []
    if not rows:
        return []
    from mini_ork.acp.history import kickoff_text, runs_by_ids
    readable: list[dict[str, str]] = []
    for r in rows:
        rid = str(r.get("id") or "")
        if not rid:
            continue
        if not kickoff_text(home, rid).strip():
            continue   # kickoff file gone → selecting it would show no rules
        readable.append({"run_id": rid, "task_class": str(r.get("task_class") or "")})
        if len(readable) >= _PREVIEW_RUNS:
            break
    if not readable:
        return []
    meta = {str(m.get("run_id")): m
            for m in runs_by_ids(home, [c["run_id"] for c in readable])}
    return [
        {"run_id": c["run_id"],
         "title": str((meta.get(c["run_id"]) or {}).get("title") or c["run_id"]),
         "task_class": c["task_class"]}
        for c in readable
    ]


def _preview_block(home: Path, conn, run_id: str, node: str) -> dict[str, Any]:
    """The exact learned block ``node`` would receive for ``run_id``'s kickoff."""
    from mini_ork.acp.history import kickoff_text, runs_by_ids
    from mini_ork.cli.prefs_cmd import build_preview

    text = kickoff_text(home, run_id, 20000)
    tc = ""
    if conn.has_table("task_runs"):
        r = conn.row("SELECT task_class FROM task_runs WHERE id = ?", (run_id,))
        tc = str(r.get("task_class") or "") if r else ""
    meta = runs_by_ids(home, [run_id])
    title = str(meta[0].get("title") or run_id) if meta else run_id
    # The preview must read THIS home's DB, not whatever MINI_ORK_DB happens to
    # name — build_preview resolves prefs/lessons from the environment.
    with scoped_environ({"MINI_ORK_HOME": str(home),
                         "MINI_ORK_DB": str(home / "state.db")}):
        block, _sources, _tc, _paths = build_preview(text, tc, node)
    body = block.strip() or ("Nothing would be given: no rules apply and no "
                             "verified lessons match this task.")
    return S.markdown(f"{node} for: {title}", body, full=True)


def _preview(home: Path, args: dict[str, str]) -> list[dict[str, Any]]:
    conn = db(home)
    node = str(args.get("node") or "implementer")
    if node not in _NODES:
        node = "implementer"
    preview = str(args.get("preview") or "").strip()
    runs = _recent_kickoff_runs(home, conn)
    run_items = [
        {"t": short(r["title"], 40), "on": r["run_id"] == preview,
         "do": S.set_args(preview=r["run_id"])}
        for r in runs
    ]
    node_items = [
        {"t": n, "on": n == node, "do": S.set_args(node=n)} for n in _NODES
    ]
    out: list[dict[str, Any]] = [
        S.chips("Pick a run", run_items, full=True,
                note="A recent run with a readable kickoff (newest first)."),
        S.chips("Pick a node", node_items, full=True),
    ]
    if preview:
        out.append(_preview_block(home, conn, preview, node))
    else:
        out.append(S.lst(
            "What will an agent be told?",
            [S.dot("Pick a run and a node to preview the exact block an agent "
                   "would be given. Read-only — nothing is logged.")],
            full=True))
    return out
