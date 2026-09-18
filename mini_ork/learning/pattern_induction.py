"""mini_ork.learning.pattern_induction — author real lessons from trace clusters.

``pattern_store.mine_from_traces`` finds clusters; it does not understand them.
It is a ``GROUP BY (task_class, status)`` whose row description is the group key
rendered as prose, so every "emergent pattern" reaching a planner prompt reads
like::

    [best_practice_rule] cluster: task_class=recipe_authoring status=success (freq=46 in window)

That is a frequency count wearing a lesson's clothes. No model has ever read
the trajectories behind it, so the block the prompt gains is a statistic the
agent cannot act on.

This module is the missing inductive step, following Trace2Skill (2603.25158)
stage 2 and stage 3:

  Stage 2 — parallel patch proposal. ``propose_lessons`` batches a cluster's
  member traces and asks an analyst to write the guidance those trajectories
  imply, once per batch, in parallel. Success and failure clusters get the same
  treatment; the sign of the lesson comes from the analyst, not from a status
  string.

  Stage 3 — consolidation. ``consolidate`` applies the paper's deterministic
  guardrails, then ``merge_lessons`` asks the model to fold the survivors into
  one coherent statement. The guardrails are structural, not semantic, because
  a judgement call is exactly what a proposer can talk its way past:

    1. format       — a lesson missing its condition or directive is dropped.
    2. provenance   — a lesson citing a trace id outside the cluster is a
                      fabrication about evidence that does not exist, and is
                      dropped. This is the guard that matters: the analyst is
                      the only thing that can name its own evidence, so the
                      only defence is to require it to be checkable.
    3. conflict     — two surviving lessons sharing a condition with opposite
                      polarity (do X / avoid X) are BOTH withheld. Nothing
                      deterministic can rank two contradictory claims, and
                      picking one by list order would make the prompt depend on
                      argument order.
    4. dedupe       — identical condition+polarity+directive collapse, their
                      evidence unioned.

  Withheld is not rejected: the caller is told which condition was quarantined
  so the contradiction is visible rather than silently resolved.

What this module does NOT do is invent evidence. A cluster whose traces carry
no readable signal (the ``tool_calls``/``files_read`` telemetry gap is real —
see ``gradient_extractor._row_has_captured_evidence``) yields no lesson, and
``lesson_text`` stays NULL. The read-back falls back to the cluster label, so a
database that predates this code behaves exactly as it did before.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from mini_ork.context import context_env

__all__ = [
    "Lesson",
    "ConsolidationResult",
    "INDEPENDENT_EVIDENCE_FLOOR",
    "consolidate",
    "induce_cluster",
    "induce_pending",
    "merge_lessons",
    "normalise_condition",
    "propose_lessons",
    "render_lesson",
]

# A lesson must cite at least this many distinct member traces. One trace is a
# single incident, and a single incident generalised into a rule is how a
# prompt acquires folklore. Mirrors reflection_pipeline's independence floor.
INDEPENDENT_EVIDENCE_FLOOR = 2

# Bound the rendered line: the block is a prompt bullet, not an essay.
_MAX_DIRECTIVE = 280
_MAX_CONDITION = 160

_POLARITIES = ("do", "avoid")

# Normalising a condition is only safe if it does not merge distinct targets.
# Digits and identifiers are preserved; casing, punctuation and whitespace are
# not meaningful in a condition, so they are folded away. "Fix the parser" and
# "fix  the parser." are the same condition; "fix parser" and "fix renderer"
# stay distinct because their words differ.
_NON_WORD = re.compile(r"[^a-z0-9]+")

# A leading connector is how a condition is phrased, not what it is about.
# Without stripping it, "When the parser sees a BOM" and "the parser sees a
# BOM" normalise to different keys and the conflict guardrail walks straight
# past two directly contradictory claims.
_LEADING_CONNECTOR = re.compile(
    r"^(?:when|whenever|if|while|during|before|after|for|in|on|at)\s+",
    re.IGNORECASE,
)


@dataclass
class Lesson:
    """One piece of guidance, with the evidence that produced it."""

    directive: str
    condition: str
    polarity: str = "do"
    evidence_trace_ids: list[str] = field(default_factory=list)
    rationale: str = ""

    @property
    def condition_key(self) -> str:
        return normalise_condition(self.condition)

    @property
    def directive_key(self) -> str:
        return normalise_condition(self.directive)


@dataclass
class ConsolidationResult:
    """Survivors plus everything the guardrails removed, with a reason each.

    The rejected list is the audit trail: an empty one means the guardrails
    found nothing to remove, which is a fact worth being able to assert.
    """

    kept: list[Lesson] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)

    @property
    def conflicts(self) -> list[dict]:
        return [r for r in self.rejected if r.get("guardrail") == "conflict"]


def normalise_condition(text: Any) -> str:
    """Fold a condition to its comparable form."""
    s = _NON_WORD.sub(" ", str(text or "").strip().lower()).strip()
    return _LEADING_CONNECTOR.sub("", s).strip()


def _clamp(text: Any, limit: int) -> str:
    """Collapse whitespace and bound length on a word boundary where possible."""
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    head = s[:limit]
    cut = head.rfind(" ")
    return (head[:cut] if cut > limit * 0.6 else head).rstrip(" ,;:-") + "…"


def _coerce_lesson(raw: Any) -> Lesson | None:
    """Build a Lesson from a model-proposed object, or None if unusable."""
    if not isinstance(raw, dict):
        return None
    polarity = str(raw.get("polarity") or "do").strip().lower()
    if polarity not in _POLARITIES:
        # An unrecognised polarity is not silently coerced to "do": "avoid" and
        # "do" are opposite instructions, and defaulting between them would
        # invert the lesson's meaning.
        return None
    ev = raw.get("evidence_trace_ids")
    if isinstance(ev, str):
        try:
            ev = json.loads(ev)
        except (ValueError, TypeError):
            ev = [ev]
    if not isinstance(ev, list):
        ev = []
    return Lesson(
        directive=_clamp(raw.get("directive"), _MAX_DIRECTIVE),
        condition=_clamp(raw.get("condition"), _MAX_CONDITION),
        polarity=polarity,
        evidence_trace_ids=[str(t) for t in ev if str(t or "").strip()],
        rationale=_clamp(raw.get("rationale"), _MAX_DIRECTIVE),
    )


def consolidate(
    proposals: Any,
    *,
    member_trace_ids: Any,
    floor: int = INDEPENDENT_EVIDENCE_FLOOR,
) -> ConsolidationResult:
    """Apply the deterministic guardrails to a proposal pool.

    Pure and order-stable: the kept list is sorted, so two runs over the same
    proposals produce the same result regardless of the order the model or the
    thread pool happened to deliver them in.

    ``floor`` may only raise the shipped constant. A caller that could pass 0
    would be able to admit single-incident lessons, which is the one thing the
    floor exists to prevent — so it is clamped, not honoured.
    """
    members = {str(t) for t in (member_trace_ids or []) if str(t or "").strip()}
    try:
        floor = max(INDEPENDENT_EVIDENCE_FLOOR, int(floor))
    except (TypeError, ValueError):
        floor = INDEPENDENT_EVIDENCE_FLOOR
    result = ConsolidationResult()

    # ── guardrail 1+2: format, then provenance ────────────────────────────
    staged: list[Lesson] = []
    for raw in proposals if isinstance(proposals, list) else []:
        lesson = _coerce_lesson(raw)
        if lesson is None:
            result.rejected.append({
                "guardrail": "format",
                "reason": "not a usable lesson object (missing polarity/directive/condition)",
                "raw": _clamp(raw, _MAX_DIRECTIVE),
            })
            continue
        if not lesson.directive or not lesson.condition:
            result.rejected.append({
                "guardrail": "format",
                "reason": "empty directive or condition",
                "raw": lesson.directive or lesson.condition,
            })
            continue
        unknown = sorted(set(lesson.evidence_trace_ids) - members)
        if unknown:
            result.rejected.append({
                "guardrail": "provenance",
                "reason": "cites traces that are not members of this cluster",
                "unknown_trace_ids": unknown[:10],
                "directive": lesson.directive,
            })
            continue
        # Distinct ids only: one trace repeated is one observation, and that is
        # the whole reason this floor exists rather than a list length.
        lesson.evidence_trace_ids = sorted(set(lesson.evidence_trace_ids))
        if len(lesson.evidence_trace_ids) < floor:
            result.rejected.append({
                "guardrail": "provenance",
                "reason": f"fewer than {floor} independent member traces",
                "directive": lesson.directive,
                "n_evidence": len(lesson.evidence_trace_ids),
            })
            continue
        staged.append(lesson)

    # ── guardrail 3: dedupe ───────────────────────────────────────────────
    deduped: dict[tuple[str, str, str], Lesson] = {}
    for lesson in staged:
        key = (lesson.condition_key, lesson.polarity, lesson.directive_key)
        if key in deduped:
            prior = deduped[key]
            prior.evidence_trace_ids = sorted(
                set(prior.evidence_trace_ids) | set(lesson.evidence_trace_ids)
            )
            continue
        deduped[key] = lesson

    # ── guardrail 4: conflict ─────────────────────────────────────────────
    by_condition: dict[str, list[Lesson]] = {}
    for lesson in deduped.values():
        by_condition.setdefault(lesson.condition_key, []).append(lesson)

    for group in sorted(by_condition.values(), key=lambda g: g[0].condition_key):
        polarities = {lesson.polarity for lesson in group}
        if len(polarities) > 1:
            for lesson in sorted(group, key=lambda x: (x.polarity, x.directive_key)):
                result.rejected.append({
                    "guardrail": "conflict",
                    "reason": (
                        "contradictory guidance for the same condition on both "
                        "sides; both withheld rather than ordered arbitrarily"
                    ),
                    "condition": lesson.condition,
                    "polarity": lesson.polarity,
                    "directive": lesson.directive,
                })
            continue
        result.kept.extend(group)

    result.kept.sort(key=lambda x: (x.condition_key, x.polarity, x.directive_key))
    return result


def render_lesson(lesson: Lesson) -> str:
    """The one-line form stored in ``lesson_text``.

    Reads as an instruction, because that is what the prompt needs. The
    condition is kept in the line: a rule without its condition is the kind of
    over-general advice that makes a memory look helpful and behave as noise.
    """
    cond = lesson.condition.strip().rstrip(":;,. ")
    cond = _LEADING_CONNECTOR.sub("", cond, count=1).strip() or cond.strip()
    if lesson.polarity == "avoid":
        return _clamp(f"When {cond}: avoid {lesson.directive}", _MAX_DIRECTIVE)
    return _clamp(f"When {cond}: {lesson.directive}", _MAX_DIRECTIVE)


# ── evidence + prompting ────────────────────────────────────────────────────

_EVIDENCE_FIELDS = (
    "trace_id", "task_class", "status", "verifier_output", "reviewer_verdict",
    "files_written", "files_read", "code_region", "reward_g",
)


def _trace_brief(row: Any) -> str:
    """One compact evidence line per trace, metadata-only.

    ``final_artifact_ref`` (a node log path) is deliberately not read here.
    Handing a proposer the raw logs invites it to quote a log line as if it
    generalised, and the log-reading path is a separate decision with its own
    cost. What is here is what the trace row already asserts about itself.
    """
    if not isinstance(row, dict):
        return ""
    parts = []
    for key in _EVIDENCE_FIELDS:
        val = row.get(key)
        if val in (None, "", "[]", "{}", "null"):
            continue
        text = str(val)
        if len(text) > 400:
            text = text[:400] + "…"
        parts.append(f"{key}={text}")
    return " | ".join(parts)


ANALYST_SYSTEM = (
    "You are an error and success analyst for a task-running agent framework. "
    "You read execution traces from ONE cluster of runs and state the guidance "
    "those traces support. You are not summarising: a summary of what happened "
    "is useless to a future run. Every lesson must tell a future agent what to "
    "DO or AVOID, and must cite only the trace ids you were given. Write the "
    "condition as the situation itself, not as a sentence opener — it is "
    "rendered as 'When <condition>', so a leading When or If would duplicate "
    "it. A lesson about a success is as valuable as one about a failure. If the "
    "traces do not support a generalisable lesson, return an empty list — an "
    "invented lesson is worse than none."
)

_MERGE_SYSTEM = (
    "You consolidate proposed guidance for a single cluster of agent runs into "
    "the smallest set of statements that preserves every distinct insight. "
    "Merge restatements. Drop anything not supported by the cited traces. If "
    "two proposals contradict each other, say so by returning neither."
)


def _lesson_contract() -> str:
    return (
        "Return ONLY a JSON object, no prose and no code fence:\n"
        '{"lessons": [{"condition": "<the situation this applies in, with no '
        'leading When/If>", '
        '"directive": "<what to do; for polarity=avoid, the action to avoid as '
        'a gerund phrase>", "polarity": "do"|"avoid", '
        '"evidence_trace_ids": ["<trace id from the list>"], '
        '"rationale": "<why, one clause>"}]}\n'
        "Cite at least two distinct trace ids per lesson. Use an empty list if "
        "nothing generalises."
    )


def _default_dispatch(
    prompt: str,
    *,
    repo_root: str | os.PathLike | None = None,
    dispatch_fn: Callable[..., int] | None = None,
    model: str | None = None,
) -> tuple[int, str]:
    """Call the native telemetry-aware dispatcher, isolating its diagnostics.

    Mirrors ``gradient_extractor._default_dispatch`` so the two learning stages
    share one transport contract and one failure shape: a non-zero rc or empty
    stdout is a failed call, never an empty-but-successful answer.
    """
    from mini_ork.dispatch import llm_dispatch as native_dispatch

    stdout, stderr = io.StringIO(), io.StringIO()
    argv = [
        "--model", model or os.environ.get("MINI_ORK_INDUCE_MODEL", "codex"),
        "--node-type", "pattern-induct",
        "--prompt-text", prompt,
        "--timeout", "120",
        "--max-turns", "4",
    ]
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = native_dispatch.llm_dispatch(
                argv,
                root=str(repo_root or os.environ.get("MINI_ORK_ROOT") or os.getcwd()),
                dispatch_fn=dispatch_fn,
            )
    except Exception:
        return 1, ""
    return rc, stdout.getvalue()


def _parse_lessons(raw: str) -> list[Any]:
    """Pull the lesson list out of a model reply, tolerating a code fence."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        # Last resort: the first balanced-looking object in the reply.
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return []
        try:
            obj = json.loads(text[start:end + 1])
        except (ValueError, TypeError):
            return []
    if isinstance(obj, dict):
        lessons = obj.get("lessons")
        return lessons if isinstance(lessons, list) else []
    return obj if isinstance(obj, list) else []


def propose_lessons(
    rows: list[Any],
    *,
    target: str,
    dispatch_fn: Callable[..., int] | None = None,
    model: str | None = None,
    batch_size: int = 6,
    max_batches: int = 4,
    max_workers: int = 4,
) -> list[Any]:
    """Stage 2 — one analyst call per batch of traces, in parallel.

    ``max_batches`` bounds cost per cluster: a cluster with 400 members is
    sampled, not fully read, because the marginal trace beyond a few batches
    changes the guidance far less than it changes the bill.
    """
    if not rows:
        return []
    size = max(1, int(batch_size))
    batches = [rows[i:i + size] for i in range(0, len(rows), size)][:max(1, max_batches)]
    if not batches:
        return []

    def _one(batch: list[Any]) -> list[Any]:
        body = "\n".join(f"- {_trace_brief(r)}" for r in batch)
        prompt = (
            f"{ANALYST_SYSTEM}\n\nCluster: {target}\n"
            f"Traces in this batch:\n{body}\n\n{_lesson_contract()}"
        )
        rc, out = _default_dispatch(prompt, dispatch_fn=dispatch_fn, model=model)
        if rc != 0:
            return []
        return _parse_lessons(out)

    workers = max(1, min(int(max_workers), len(batches)))
    if workers == 1:
        return [p for b in batches for p in _one(b)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return [p for chunk in pool.map(_one, batches) for p in chunk]


def merge_lessons(
    lessons: list[Lesson],
    *,
    target: str,
    dispatch_fn: Callable[..., int] | None = None,
    model: str | None = None,
) -> Lesson | None:
    """Stage 3 — fold the survivors into one coherent statement.

    Falls back to the deterministic choice (most independent evidence, then
    stable key order) when the merge call fails or returns nothing usable. The
    fallback is not a lesser code path: it is what makes the stage non-fatal,
    since a prompt block must never depend on a model call succeeding.
    """
    if not lessons:
        return None

    def _fallback() -> Lesson:
        return sorted(
            lessons,
            key=lambda x: (-len(x.evidence_trace_ids), x.condition_key, x.directive_key),
        )[0]

    if len(lessons) == 1:
        return lessons[0]

    body = "\n".join(
        json.dumps({
            "condition": x.condition, "directive": x.directive,
            "polarity": x.polarity, "evidence_trace_ids": x.evidence_trace_ids,
        }, sort_keys=True)
        for x in lessons
    )
    prompt = (
        f"{_MERGE_SYSTEM}\n\nCluster: {target}\nProposals:\n{body}\n\n{_lesson_contract()}"
    )
    rc, out = _default_dispatch(prompt, dispatch_fn=dispatch_fn, model=model)
    if rc != 0:
        return _fallback()
    merged = [x for x in (_coerce_lesson(x) for x in _parse_lessons(out)) if x is not None]
    if not merged:
        return _fallback()
    # The merge output is still a proposal, and gets no discount for arriving
    # last: it goes back through the same guardrails. `allowed` is the set of
    # traces its inputs already cited, so passing it as the member set enforces
    # both halves at once — the merged lesson may not cite a trace no analyst
    # ever cited (the laundering channel), and it must clear the same floor as
    # everything else (a merge that compresses three lessons into one claim
    # citing a single trace has lost the independence, not earned an exemption).
    allowed = sorted({t for x in lessons for t in x.evidence_trace_ids})
    rechecked = consolidate(
        [
            {
                "condition": x.condition, "directive": x.directive,
                "polarity": x.polarity, "evidence_trace_ids": x.evidence_trace_ids,
                "rationale": x.rationale,
            }
            for x in merged
        ],
        member_trace_ids=allowed,
    )
    if not rechecked.kept:
        return _fallback()
    return sorted(
        rechecked.kept,
        key=lambda x: (-len(x.evidence_trace_ids), x.condition_key, x.directive_key),
    )[0]


# ── orchestration against the DB ────────────────────────────────────────────

def _induct_enabled() -> bool:
    """Default ON with an opt-out, per the house rule for new capability."""
    return os.environ.get("MO_PATTERN_INDUCE", "1") == "1"


def _read_cluster(con: sqlite3.Connection, trace_ids: list[str]) -> list[dict]:
    if not trace_ids:
        return []
    placeholders = ",".join("?" * len(trace_ids))
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(
            f"SELECT {','.join(_EVIDENCE_FIELDS)} FROM execution_traces "
            f"WHERE trace_id IN ({placeholders})",
            tuple(trace_ids),
        ).fetchall()]
    except sqlite3.OperationalError:
        return []


def induce_cluster(
    con: sqlite3.Connection,
    *,
    target: str,
    member_trace_ids: list[str],
    dispatch_fn: Callable[..., int] | None = None,
    model: str | None = None,
) -> tuple[str, dict]:
    """Author the lesson text for one cluster. Returns (lesson_text, report)."""
    rows = _read_cluster(con, member_trace_ids)
    if not rows:
        return "", {"target": target, "reason": "no readable member traces"}

    proposals = propose_lessons(rows, target=target, dispatch_fn=dispatch_fn, model=model)
    result = consolidate(proposals, member_trace_ids=member_trace_ids)
    if not result.kept:
        return "", {
            "target": target,
            "n_proposals": len(proposals),
            "rejected": result.rejected[:10],
            "reason": "no proposal survived the guardrails",
        }
    merged = merge_lessons(
        result.kept, target=target, dispatch_fn=dispatch_fn, model=model,
    )
    if merged is None:
        return "", {"target": target, "reason": "merge produced nothing"}
    return render_lesson(merged), {
        "target": target,
        "n_proposals": len(proposals),
        "n_kept": len(result.kept),
        "n_conflicts_withheld": len(result.conflicts),
        "lesson": render_lesson(merged),
    }


def induce_pending(
    *,
    db_path: str | None = None,
    limit: int = 20,
    min_cluster: int = 3,
    dispatch_fn: Callable[..., int] | None = None,
    model: str | None = None,
) -> dict:
    """Author lessons for clusters that do not have one yet.

    Scoped to ``pattern_records`` rows whose ``lesson_text`` is NULL, so a
    re-run is cheap and an authored lesson is never overwritten by a second
    opinion. Overwriting is a separate, deliberate act.

    There is no time window here: a window would re-derive the same clusters
    every pass (``pattern_id`` is a hash of the cluster key, so it is stable
    across re-mining) and re-ask the same question of the same evidence. What
    gates this is whether a lesson already exists.
    """
    report: dict[str, Any] = {"induced": 0, "skipped": [], "enabled": _induct_enabled()}
    if not _induct_enabled():
        return report

    from mini_ork.stores import pattern_store

    db = db_path or context_env("MINI_ORK_DB", "") or os.path.join(
        context_env("MINI_ORK_HOME", "") or os.path.join(os.getcwd(), ".mini-ork"),
        "state.db",
    )
    if not os.path.isfile(db):
        return report

    pattern_store.ensure_lesson_columns(db)

    con = sqlite3.connect(db)
    con.execute("PRAGMA busy_timeout=5000")
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT pattern_id, description, evidence_trace_ids
              FROM pattern_records
             WHERE COALESCE(lesson_text, '') = ''
             ORDER BY frequency DESC
             LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        for row in rows:
            pid = row["pattern_id"]
            raw_ev = row["evidence_trace_ids"] or "[]"
            try:
                members = json.loads(raw_ev)
            except (ValueError, TypeError):
                members = []
            members = [str(t) for t in members if str(t or "").strip()]
            if len(set(members)) < max(1, int(min_cluster)):
                report["skipped"].append({"pattern_id": pid, "reason": "too few members"})
                continue
            # Oldest evidence first: the cluster key is stable across re-mining
            # but the member list grows, so capping to the newest N would make
            # the lesson drift every window for no reason.
            members = members[-max(1, min_cluster * 4):]
            text, detail = induce_cluster(
                con, target=row["description"] or pid, member_trace_ids=members,
                dispatch_fn=dispatch_fn, model=model,
            )
            if not text:
                report["skipped"].append(detail)
                continue
            con.execute(
                "UPDATE pattern_records SET lesson_text=? WHERE pattern_id=?",
                (text, pid),
            )
            # Propagate at authoring time. The judge-gate row is what the
            # prompt block actually reads, and the pass that persists it
            # (`reflection_persist_suggestions`) runs before this one — so
            # without this write the lesson would reach the agent one reflect
            # later, and a cluster never re-mined would never carry it at all.
            # Only a row with no lesson is filled; an authored one is never
            # overwritten by a second opinion.
            try:
                con.execute(
                    "UPDATE emergent_patterns SET lesson_text=? "
                    "WHERE pattern_id=? AND COALESCE(lesson_text,'')=''",
                    (text, pid),
                )
            except sqlite3.OperationalError:
                pass
            con.commit()
            report["induced"] += 1
        return report
    finally:
        con.close()
