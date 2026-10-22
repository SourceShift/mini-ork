"""Conflict check between a candidate lesson and the lessons already live.

Trace2Skill consolidates lessons distilled from many traces. mini-ork writes
lessons in two places — ``pattern_induction.induce_pending`` (``pattern_records``
+ ``emergent_patterns``) and ``themes.promote`` (``lesson_themes``) — and both
share the same grounded author (``induce_cluster``). Neither compared the new
text against the lessons already live for the same task class, so two
contradictory lessons could both reach a prompt and near-duplicates piled up.

This module is the one check between authoring and the write:

1. ``live_lessons`` collects the already-authored lessons relevant to a task
   class from both stores. A source whose table or column is missing (a
   pre-migration database) is skipped, never fatal.
2. ``neighbours`` ranks those lessons by token containment, so the judge only
   sees the handful a new lesson could plausibly duplicate or contradict.
3. ``check`` asks the same dispatch path ``induce_cluster`` uses — one call for
   all neighbours — for a strict-JSON relation per neighbour. Any
   ``contradicts`` blocks as ``conflict``; otherwise any ``duplicate`` blocks as
   ``duplicate``; ``refines`` and ``independent`` never block. Every
   non-independent relation is recorded in ``lesson_conflicts``, and a conflict
   also enqueues exactly one ``lesson-conflict`` oversight item.

The stance is the same as ``consolidate``'s in-cluster conflict guardrail:
withhold, never order arbitrarily, and fail closed. A dispatch failure or an
unparseable reply is ``unchecked``, and the caller must not write the lesson —
the row stays unauthored and is retried on the next pass. That means an LLM
outage halts lesson authoring; ``MO_LESSON_CONFLICT_CHECK=0`` is the
operational escape hatch (only the exact string ``"0"`` disables the check).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from typing import Any, Callable

from mini_ork.gates import oversight_inbox

__all__ = ["enabled", "live_lessons", "neighbours", "check", "skip_reason"]

GATE_ID = "lesson-conflict"

# What the two writers put in ``report["skipped"][i]["reason"]``. ``unchecked``
# is the only other verdict a writer sees; it means the judge could not answer,
# so it has its own reason. The strings are contract: the operator counts
# "conflict check unavailable" to see a judge outage.
_SKIP_REASONS = {
    "duplicate": "lesson duplicate",
    "conflict": "lesson conflict",
}
UNCHECKED_REASON = "conflict check unavailable"


def skip_reason(verdict: str) -> str:
    """The report reason for a non-``ok`` verdict (``unchecked`` by default)."""
    return _SKIP_REASONS.get(verdict, UNCHECKED_REASON)

# Verbatim copy of db/migrations/0083_lesson_conflicts.sql: the table and its
# unique key. Re-running is a no-op; a database that never runs the migration
# runner still gets the table on the first ``check`` that records a relation.
_BOOTSTRAP_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS lesson_conflicts (
      id             INTEGER PRIMARY KEY,
      source_kind    TEXT NOT NULL,
      source_id      TEXT NOT NULL,
      against_kind   TEXT NOT NULL,
      against_id     TEXT NOT NULL,
      relation       TEXT NOT NULL,
      why            TEXT,
      candidate_text TEXT,
      created_at     INTEGER NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_lesson_conflicts_pair
      ON lesson_conflicts(source_kind, source_id, against_kind, against_id)
    """,
)

# Lowercase word tokens. Kept self-contained on purpose: ``themes._tokenize``
# normalises identifiers and drops stopwords/short tokens, which is a different
# token set and a different metric's input.
_TOKEN_RE = re.compile(r"[a-z0-9_]+")

# The relations the judge may return. ``independent`` and ``refines`` never
# block a write; the other two do.
_RELATIONS = ("duplicate", "contradicts", "refines", "independent")


def enabled() -> bool:
    """Default ON with an opt-out — only the exact string ``"0"`` disables it.

    Deliberately NOT the neighbouring ``_induct_enabled()`` shape
    (``== "1"``): that one is opt-in, and copying it here would ship the check
    off by default.
    """
    return os.environ.get("MO_LESSON_CONFLICT_CHECK", "1") != "0"


# ── live lessons ────────────────────────────────────────────────────────────


def _class_is_relevant(stored: Any, target: str | None) -> bool:
    """A stored lesson is relevant when its class matches or is global.

    ``'*'``, NULL and empty are the explicit "applies to every class" values.
    Equality is literal: ``lesson_themes.task_class`` can be comma-joined
    (``refresh_theme`` writes ``",".join(sorted(classes))``); widening this to
    token matching would change what "same class" means for every reader.
    """
    if stored is None:
        return True
    value = str(stored).strip()
    if value in ("", "*"):
        return True
    return value == (target or "")


def live_lessons(con: sqlite3.Connection, task_class: str | None) -> list[dict]:
    """Already-authored lessons relevant to ``task_class``.

    Each item is ``{"kind", "id", "text"}``. Sources:

    * ``lesson_themes`` rows with ``status='verified'`` and non-blank text;
    * ``emergent_patterns`` rows with non-blank text (any status — the pattern
      store has no verification column shared with themes).

    A missing table **or column** skips that source: a database that predates
    0056 (``lesson_text``) or 0077 (``task_class``) still serves whatever it
    can, and ``induce_pending``'s healing path keeps working. The columns are
    selected positionally so this works with or without ``row_factory=Row``.
    """
    lessons: list[dict] = []
    try:
        rows = con.execute(
            "SELECT theme_id, lesson_text, task_class FROM lesson_themes "
            "WHERE status = 'verified'"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for row in rows:
        text = row[1]
        if text is None or not str(text).strip():
            continue
        if not _class_is_relevant(row[2], task_class):
            continue
        lessons.append({"kind": "theme", "id": str(row[0]), "text": str(text)})

    try:
        rows = con.execute(
            "SELECT pattern_id, lesson_text, task_class FROM emergent_patterns"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for row in rows:
        text = row[1]
        if text is None or not str(text).strip():
            continue
        if not _class_is_relevant(row[2], task_class):
            continue
        lessons.append({"kind": "pattern", "id": str(row[0]), "text": str(text)})
    return lessons


# ── neighbours ──────────────────────────────────────────────────────────────


def _tokens(text: Any) -> set[str]:
    return set(_TOKEN_RE.findall(str(text or "").lower()))


def _containment(a: set[str], b: set[str]) -> float:
    """``|A∩B| / min(|A|,|B|)`` over token sets.

    A set intersection, not a multiset count: a lesson that repeats one word
    must not inflate its score. ``min`` makes a short lesson that is wholly
    contained in a longer candidate score 1.0, which is the intended reading
    (the candidate restates the short lesson).
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def neighbours(
    text: str,
    lessons: list[dict],
    *,
    k: int = 5,
    min_overlap: float = 0.3,
) -> list[dict]:
    """The ``k`` closest live lessons to ``text`` at ``>= min_overlap``.

    Ranked by containment descending, then id, so the order is deterministic
    across runs. Ties are broken on the id rather than the input order — a
    judge that sees a different neighbour set every pass would make the check
    nondeterministic for no reason.
    """
    candidate = _tokens(text)
    scored: list[tuple[float, str, dict]] = []
    for lesson in lessons or []:
        score = _containment(candidate, _tokens(lesson.get("text")))
        if score >= min_overlap:
            scored.append((score, str(lesson.get("id", "")), lesson))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [lesson for _score, _id, lesson in scored[: max(0, int(k))]]


# ── the judge call ──────────────────────────────────────────────────────────


def _conflict_prompt(text: str, near: list[dict]) -> str:
    lines = [
        "You audit a candidate lesson written for a task-running agent "
        "framework against the lessons already live for the same task class.",
        "Decide, for each numbered neighbour, how the candidate relates to "
        "that lesson.",
        "",
        "Relations (pick exactly one per neighbour):",
        '  "duplicate"   — the candidate restates the neighbour',
        '  "contradicts" — the candidate gives incompatible guidance for the '
        "same situation",
        '  "refines"     — the candidate narrows or extends the neighbour '
        "without contradicting it",
        '  "independent" — a distinct lesson that can coexist',
        "",
        "Return ONLY a JSON array, no prose and no code fence:",
        '[{"id": "<the neighbour id shown below>", "relation": '
        '"duplicate"|"contradicts"|"refines"|"independent", '
        '"why": "<one clause>"}]',
        "",
        f"Candidate lesson: {text}",
        "",
        "Live neighbours:",
    ]
    for i, lesson in enumerate(near, 1):
        lines.append(
            f'{i}. id={lesson.get("id")} ({lesson.get("kind")}): '
            f'{lesson.get("text")}'
        )
    return "\n".join(lines)


def _default_dispatch(
    prompt: str,
    *,
    dispatch_fn: Callable[..., int] | None = None,
    model: str | None = None,
) -> tuple[int, str]:
    """Resolve the induction dispatch path as a value, never by import.

    Production passes ``model=`` but not ``dispatch_fn`` (``cli/reflect.py``),
    so ``dispatch_fn=None`` is the production default and here means "use the
    lane ``induce_cluster`` uses". Resolving it lazily through the module keeps
    a test's monkeypatch of ``pattern_induction._default_dispatch`` effective;
    a ``from ... import`` would bind at import time and bypass every stub.
    """
    from mini_ork.learning import pattern_induction as pi

    return pi._default_dispatch(prompt, dispatch_fn=dispatch_fn, model=model)


def _parse_relations(raw: str) -> list[dict] | None:
    """Strict parse of the judge reply; ``None`` means "not a usable answer".

    The reply must be a JSON array whose every item is an object carrying a
    recognised relation. Anything else is ``unchecked``, never a guess: the
    authoring prompts also return JSON, and treating a ``{"lessons": [...]}``
    payload as "no relations" would silently write a lesson past the gate.
    """
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end <= start:
            return None
        try:
            obj = json.loads(text[start:end + 1])
        except (ValueError, TypeError):
            return None
    if not isinstance(obj, list):
        return None
    out: list[dict] = []
    for item in obj:
        if not isinstance(item, dict):
            return None
        relation = item.get("relation")
        if not isinstance(relation, str):
            return None
        relation = relation.strip().lower()
        if relation not in _RELATIONS:
            return None
        out.append({
            "id": item.get("id"),
            "relation": relation,
            "why": item.get("why"),
        })
    return out


def _match_lesson(item_id: Any, near: list[dict]) -> dict | None:
    """Map a judge item back to a neighbour; unknown ids are ignored.

    Accepts the id string the prompt asks for, and a 1-based number as a
    tolerance for a judge that echoes the list position instead.
    """
    if item_id is None or isinstance(item_id, bool):
        return None
    if isinstance(item_id, int):
        idx = item_id - 1
        return near[idx] if 0 <= idx < len(near) else None
    key = str(item_id).strip()
    for lesson in near:
        if str(lesson.get("id")) == key:
            return lesson
    if key.isdigit():
        idx = int(key) - 1
        return near[idx] if 0 <= idx < len(near) else None
    return None


# ── recording ───────────────────────────────────────────────────────────────


def _ensure_table(con: sqlite3.Connection) -> None:
    for statement in _BOOTSTRAP_STATEMENTS:
        con.execute(statement)


def _record_relations(
    con: sqlite3.Connection,
    *,
    source_kind: str,
    source_id: str,
    text: str,
    relations: list[dict],
) -> None:
    recorded = [r for r in relations if r["relation"] != "independent"]
    if not recorded:
        return
    _ensure_table(con)
    created_at = int(time.time())
    for relation in recorded:
        con.execute(
            "INSERT OR IGNORE INTO lesson_conflicts "
            "(source_kind, source_id, against_kind, against_id, relation, "
            " why, candidate_text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_kind, str(source_id), relation["kind"],
                str(relation["id"]), relation["relation"],
                relation.get("why"), text, created_at,
            ),
        )
    # Commit before any oversight enqueue: ``enqueue`` opens its own
    # connection, and an uncommitted write on this one would leave it blocked
    # on SQLite's single writer (``busy_timeout`` then turns into a raise).
    con.commit()


def _con_db_path(con: sqlite3.Connection) -> str | None:
    """The file backing ``con``'s ``main`` database, or the env fallback.

    ``oversight_inbox.enqueue`` takes no connection; without this it would
    write to ``$MINI_ORK_DB`` even when the caller's ``con`` is a temp database,
    and a hermetic test would then fail to find the row it just wrote.
    """
    try:
        rows = con.execute("PRAGMA database_list").fetchall()
    except sqlite3.OperationalError:
        rows = []
    for row in rows:
        if row[1] == "main" and row[2]:
            return str(row[2])
    return os.environ.get("MINI_ORK_DB") or None


def _oversight_exists(con: sqlite3.Connection, feature: str) -> bool:
    """Whether a ``lesson-conflict`` item for ``feature`` exists in any status.

    ``oversight_inbox.pending()`` filters on ``status='pending'``, so it cannot
    answer this. The table may legitimately be absent on the caller's
    connection (a minimal database); that is "not enqueued", not an error.
    """
    try:
        row = con.execute(
            "SELECT 1 FROM mo_inbox_gates "
            "WHERE gate_id = ? AND feature = ? LIMIT 1",
            (GATE_ID, feature),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def _enqueue_conflict(
    con: sqlite3.Connection, source_kind: str, source_id: str
) -> None:
    feature = f"lesson conflict: {source_kind} {source_id}"
    if _oversight_exists(con, feature):
        return
    oversight_inbox.enqueue(GATE_ID, feature, db_path=_con_db_path(con))


# ── public entry point ──────────────────────────────────────────────────────


def check(
    con: sqlite3.Connection,
    text: str,
    *,
    task_class: str | None,
    source_kind: str,
    source_id: str,
    dispatch_fn: Callable[..., int] | None = None,
    model: str | None = None,
) -> dict:
    """Judge a candidate lesson against its live neighbours.

    Returns ``{"verdict": "ok"|"duplicate"|"conflict"|"unchecked",
    "relations": [...]}``. With no neighbours the verdict is ``ok`` and the
    judge is never called — that fast path is load-bearing for every caller
    whose database has no live lessons yet, and it is also why the check costs
    nothing on a cold store.
    """
    near = neighbours(text, live_lessons(con, task_class))
    if not near:
        return {"verdict": "ok", "relations": []}

    try:
        rc, raw = _default_dispatch(
            _conflict_prompt(text, near), dispatch_fn=dispatch_fn, model=model
        )
    except Exception:
        return {"verdict": "unchecked", "relations": []}
    if rc != 0:
        return {"verdict": "unchecked", "relations": []}
    parsed = _parse_relations(raw)
    if parsed is None:
        return {"verdict": "unchecked", "relations": []}

    relations: list[dict] = []
    for item in parsed:
        lesson = _match_lesson(item.get("id"), near)
        if lesson is None:
            continue
        relations.append({
            "kind": lesson.get("kind"),
            "id": lesson.get("id"),
            "relation": item["relation"],
            "why": item.get("why"),
        })
    if not relations:
        # A well-formed reply that classifies none of the neighbours shown
        # (empty array, or every id unknown) judged nothing: fail closed rather
        # than let the candidate through as "ok".
        return {"verdict": "unchecked", "relations": []}

    verdict = "ok"
    if any(r["relation"] == "contradicts" for r in relations):
        verdict = "conflict"
    elif any(r["relation"] == "duplicate" for r in relations):
        verdict = "duplicate"

    try:
        _record_relations(
            con,
            source_kind=source_kind,
            source_id=source_id,
            text=text,
            relations=relations,
        )
        if verdict == "conflict":
            _enqueue_conflict(con, source_kind, source_id)
    except sqlite3.OperationalError:
        # A malformed ``lesson_conflicts`` (or an unwritable oversight store)
        # means the check could not be completed as specified: fail closed.
        return {"verdict": "unchecked", "relations": relations}

    return {"verdict": verdict, "relations": relations}
