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

The detector is wired onto the *authoring* path, so it only ever saw lessons
written after it landed. ``audit_live_corpus`` is the missing half: one capped
pass that walks the lessons already live and judges them against each other
through the same ``check``. Every row it lands is written by this module's own
writer, so the evidence and the detector can never drift apart.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from typing import Any, Callable

from mini_ork.gates import oversight_inbox

__all__ = [
    "enabled",
    "live_lessons",
    "neighbours",
    "check",
    "skip_reason",
    "audit_live_corpus",
    "audit_limit",
    "DEFAULT_AUDIT_LIMIT",
]

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

# The cap for one audit pass. The corpus is ~200 lessons today and every
# source costs one judge call, so an unbounded pass is a bill, not a check.
# ``MO_LESSON_CONFLICT_AUDIT_LIMIT`` overrides it; ``0`` disables the pass.
DEFAULT_AUDIT_LIMIT = 20
AUDIT_LIMIT_ENV = "MO_LESSON_CONFLICT_AUDIT_LIMIT"


def enabled() -> bool:
    """Default ON with an opt-out — only the exact string ``"0"`` disables it.

    Deliberately NOT the neighbouring ``_induct_enabled()`` shape
    (``== "1"``): that one is opt-in, and copying it here would ship the check
    off by default.
    """
    return os.environ.get("MO_LESSON_CONFLICT_CHECK", "1") != "0"


# ── live lessons ────────────────────────────────────────────────────────────


def _class_members(value: Any) -> set[str]:
    """The member classes of a stored or target class value.

    ``lesson_themes.task_class`` is a comma-joined member set
    (``refresh_theme`` writes ``",".join(sorted(classes))``), so a value may
    name several classes at once.
    """
    return {part.strip() for part in str(value or "").split(",") if part.strip()}


def _class_is_relevant(stored: Any, target: str | None) -> bool:
    """A stored lesson is relevant when its class shares a member with the target.

    ``lesson_themes.task_class`` is a comma-joined *member set*
    (``refresh_theme`` writes ``code_fix,framework_edit``) and the lesson
    reader already matches those as tier-1 (``themes._tier``). Comparing whole
    strings therefore hid most of the corpus: a multi-class theme matched
    nothing but itself and the global lessons, so the corpus split into
    disconnected islands.

    The rule here is the symmetric one — the two member sets must
    **intersect**: ``members(stored) & members(target)``. The reader's tier-1
    form tests one side only, which is a subset of this and starves a
    multi-class source of neighbours entirely.

    The legacy corners are unchanged:

    * a stored ``NULL`` / ``''`` / ``'*'`` class is global — it matches any
      target;
    * a global *target* (``''`` / ``'*'``) matches only the global stored
      rows. ``pattern_induction`` documents ``'*'`` as narrowing the
      neighbour set to the genuinely cross-class lessons.
    """
    if stored is None:
        return True
    value = str(stored).strip()
    if value in ("", "*"):
        return True
    target_value = "" if target is None else str(target).strip()
    if target_value in ("", "*"):
        return False
    return bool(_class_members(value) & _class_members(target_value))


def _corpus(con: sqlite3.Connection) -> list[dict]:
    """Every live-corpus row, with the class it was filed under.

    One definition, shared by ``live_lessons`` (which filters it by class) and
    the audit pass (which walks all of it): ``lesson_themes`` rows with
    ``status='verified'`` and a non-blank ``lesson_text``, plus
    ``emergent_patterns`` rows with a non-blank lesson (any status — the
    pattern store has no verification column shared with themes). A missing
    table **or column** skips that source, never fatal. The columns are
    selected positionally so this works with or without ``row_factory=Row``.
    """
    corpus: list[dict] = []
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
        corpus.append({
            "kind": "theme",
            "id": str(row[0]),
            "text": str(text),
            "task_class": row[2],
        })

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
        corpus.append({
            "kind": "pattern",
            "id": str(row[0]),
            "text": str(text),
            "task_class": row[2],
        })
    return corpus


def live_lessons(
    con: sqlite3.Connection,
    task_class: str | None,
    *,
    exclude: tuple[str, str] | None = None,
) -> list[dict]:
    """Already-authored lessons relevant to ``task_class``.

    Each item is ``{"kind", "id", "text"}``. Sources:

    * ``lesson_themes`` rows with ``status='verified'`` and non-blank text;
    * ``emergent_patterns`` rows with non-blank text (any status — the pattern
      store has no verification column shared with themes).

    A missing table **or column** skips that source: a database that predates
    0056 (``lesson_text``) or 0077 (``task_class``) still serves whatever it
    can, and ``induce_pending``'s healing path keeps working. The columns are
    selected positionally so this works with or without ``row_factory=Row``.

    ``exclude`` drops one ``(kind, id)`` row from the result. The audit pass
    uses it so a source is never its own neighbour; on the authoring path the
    candidate is not live yet, so it is a no-op there.
    """
    lessons: list[dict] = []
    for row in _corpus(con):
        if exclude is not None and (row["kind"], row["id"]) == exclude:
            continue
        if not _class_is_relevant(row["task_class"], task_class):
            continue
        lessons.append({"kind": row["kind"], "id": row["id"], "text": row["text"]})
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
    # A relation the judge gave no reason for is not evidence: the row would
    # land with a NULL ``why`` and say nothing about why two lessons relate.
    # The verdict is still computed from every relation the judge returned —
    # dropping the row never weakens the guardrail on the authoring path.
    recorded = [
        r for r in relations
        if r["relation"] != "independent" and str(r.get("why") or "").strip()
    ]
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
    exclude: tuple[str, str] | None = None,
) -> dict:
    """Judge a candidate lesson against its live neighbours.

    Returns ``{"verdict": "ok"|"duplicate"|"conflict"|"unchecked",
    "relations": [...]}``. With no neighbours the verdict is ``ok`` and the
    judge is never called — that fast path is load-bearing for every caller
    whose database has no live lessons yet, and it is also why the check costs
    nothing on a cold store.

    ``exclude`` drops one ``(kind, id)`` row from the neighbour set. The audit
    pass uses it to keep a source from being judged against its own text; on
    the authoring path the candidate is not live yet, so it changes nothing.
    """
    near = neighbours(text, live_lessons(con, task_class, exclude=exclude))
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


# ── the audit pass ──────────────────────────────────────────────────────────


def audit_limit(default: int = DEFAULT_AUDIT_LIMIT) -> int:
    """The cap for one audit pass: ``$MO_LESSON_CONFLICT_AUDIT_LIMIT`` or 20.

    ``0`` turns the pass off (it judges nothing) — the operator's way to
    keep the spend down without turning the whole check off. A malformed
    value falls back to the default rather than crashing a side-channel.
    """
    raw = os.environ.get(AUDIT_LIMIT_ENV)
    if raw is None:
        return default
    text = str(raw).strip()
    if not text:
        return default
    try:
        value = int(text)
    except ValueError:
        return default
    return max(0, value)


def _audit_order(corpus: list[dict]) -> list[dict]:
    """The corpus ordered by best containment against the *other* rows.

    The order is the point of the pass: the sources that restate another
    lesson are judged first, so a capped pass spends its budget where the
    evidence is. A source is scored against every other row, so the top of
    the order is the pair that says the most nearly the same thing.

    Ties break on ``(kind, id)``, so two passes over the same corpus judge
    the same sources in the same order — the pass is deterministic, not
    merely reproducible.
    """
    token_sets = [_tokens(row["text"]) for row in corpus]
    scored: list[tuple[float, str, str, dict]] = []
    for index, source in enumerate(corpus):
        best = 0.0
        for other_index, other_tokens in enumerate(token_sets):
            if other_index == index:
                continue
            score = _containment(token_sets[index], other_tokens)
            if score > best:
                best = score
        scored.append((-best, source["kind"], source["id"], source))
    scored.sort(key=lambda item: item[:3])
    return [item[3] for item in scored]


def _conflict_row_count(con: sqlite3.Connection) -> int:
    """How many relations the table holds; ``0`` when it was never created."""
    try:
        row = con.execute("SELECT count(*) FROM lesson_conflicts").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row and row[0] is not None else 0


def _audit_one(
    con: sqlite3.Connection,
    source: dict,
    *,
    dispatch_fn: Callable[..., int] | None,
    model: str | None,
) -> dict:
    """Judge one source against the live corpus. Never raises.

    The source is excluded from its own neighbour set, so a relation always
    names two different rows — a source judged against its own text is a
    circle, not evidence.
    """
    outcome = {"verdict": "unchecked", "judged": False, "relations": []}
    try:
        result = check(
            con,
            source["text"],
            task_class=source["task_class"] or "*",
            source_kind=source["kind"],
            source_id=source["id"],
            dispatch_fn=dispatch_fn,
            model=model,
            exclude=(source["kind"], source["id"]),
        )
    except Exception:
        # A judge that cannot be reached is a fact about the judge, not a
        # crash of the audit: record nothing, withhold nothing, go on.
        return outcome
    verdict = result.get("verdict") if isinstance(result, dict) else None
    relations = result.get("relations") if isinstance(result, dict) else None
    if verdict not in ("ok", "duplicate", "conflict", "unchecked"):
        verdict = "unchecked"
    outcome["verdict"] = verdict
    outcome["relations"] = relations if isinstance(relations, list) else []
    # ``ok`` with no relation means there was no neighbour to judge — the
    # judge was never called for this source.
    outcome["judged"] = bool(outcome["relations"]) or verdict != "ok"
    if verdict == "duplicate":
        # On the authoring path a duplicate is withheld and retried, so the
        # operator never hears about it. Here the source is a lesson that has
        # been live for months: "duplicate" means two live lessons say the
        # same thing, which is worth an operator's attention. The module's
        # own writer enqueues it (one item per feature, idempotent).
        try:
            _enqueue_conflict(con, source["kind"], source["id"])
        except Exception:
            pass
    return outcome


def audit_live_corpus(
    db_path: str | os.PathLike[str],
    *,
    limit: int = DEFAULT_AUDIT_LIMIT,
    dispatch_fn: Callable[..., int] | None = None,
    model: str | None = None,
) -> dict:
    """Judge the lessons that are already live against each other.

    The detector is wired onto the *authoring* path — ``themes.promote`` and
    ``pattern_induction.induce_pending`` call ``check`` before a new lesson is
    written — so the lessons that predate that wiring were never examined.
    A corpus can hold two contradicting lessons and nothing notices.

    This pass walks the live corpus (``_corpus``: verified themes with a
    lesson, plus patterns with a lesson), orders the sources by their best
    containment against the other rows, and calls the **existing** ``check``
    once per source. Every row that lands is therefore written by the
    detector's own writer (``_record_relations``): this function never
    inserts into ``lesson_conflicts`` itself, and a relation always names two
    real rows with the judge's reason attached.

    * ``limit`` caps the judge calls — at most one per source, in the order
      above, so the pass is bounded and deterministic.
    * A re-run re-judges the same head of the order. Recording is idempotent
      through ``ux_lesson_conflicts_pair``, so a re-run adds no row; it does
      not refresh a stored relation either (the first verdict stands).
    * ``unchecked`` — a judge outage, an unparseable reply — records nothing,
      withholds no lesson (the sources are already live; nothing here writes
      to them) and does not stop the pass. The next source is judged.
    * The corpus is the one ``live_lessons`` reads: patterns are read with no
      status filter, so a proposed pattern is audited too. The prompt block
      injects only ``status='approved'`` patterns (``context_assembler``), so
      a relation against a proposed pattern describes a lesson the prompt may
      not show. That asymmetry is pre-existing and deliberate here — the audit
      is about the corpus the detector sees.

    Returns a report dict: ``{"audited", "judged", "relations", "recorded",
    "limit", "verdicts", "sources"}``. ``recorded`` counts the rows the pass
    actually added, which is ``0`` on a re-run.
    """
    try:
        cap = max(0, int(limit))
    except (TypeError, ValueError):
        cap = DEFAULT_AUDIT_LIMIT
    report: dict[str, Any] = {
        "audited": 0,
        "judged": 0,
        "relations": 0,
        "recorded": 0,
        "limit": cap,
        "verdicts": {"ok": 0, "duplicate": 0, "conflict": 0, "unchecked": 0},
        "sources": [],
    }
    path = str(db_path or "")
    if not path or not os.path.isfile(path):
        report["error"] = "database not found"
        return report

    con = sqlite3.connect(path, timeout=5.0)
    try:
        con.row_factory = sqlite3.Row
        # The live database is shared; a concurrent writer turns into an
        # OperationalError without this, and that would swallow the pass.
        con.execute("PRAGMA busy_timeout=5000")
        corpus = _corpus(con)
        rows_before = _conflict_row_count(con)
        for source in _audit_order(corpus)[:cap]:
            outcome = _audit_one(con, source, dispatch_fn=dispatch_fn, model=model)
            report["audited"] += 1
            verdict = outcome["verdict"]
            report["verdicts"][verdict] = report["verdicts"].get(verdict, 0) + 1
            if outcome["judged"]:
                report["judged"] += 1
            report["relations"] += sum(
                1 for r in outcome["relations"]
                if r.get("relation") != "independent"
            )
            report["sources"].append({
                "kind": source["kind"],
                "id": source["id"],
                "verdict": verdict,
                "relations": [
                    {"against": r.get("id"), "relation": r.get("relation")}
                    for r in outcome["relations"]
                ],
            })
        report["recorded"] = max(0, _conflict_row_count(con) - rows_before)
    finally:
        con.close()
    return report
