"""Per-run task state for the ACP thread surface (Zed S1).

A single ``task_state(run_dir, snapshot)`` call answers the kickoff's
five-rule question — *what is this run doing right now, and is the
user needed?* — without any I/O beyond the ``.cost-pause`` sentinel
and the cached diff list. ``run_mark`` is the cheap sibling used by
``list_sessions`` for the run rows in the thread list (no event /
diff reads); ``title_with_state`` formats the title that lands in
Zed's thread list.

Pure functions, no async, no ACP types, no module-level state — same
shape as ``mini_ork.acp.diffs`` (``diffs.py:15-16``: "Pure functions:
no ACP types, no async, no module-level state"). The dataclass is
``frozen=True`` so callers can hash and compare it cheaply and tests
can pin exact equality.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Mark glyphs prefixed to a thread's title; the unicode minus in the
# diff suffix (U+2212, "−") is NOT the ASCII "-" — copy the literal.
MARKS: dict[str, str] = {
    "working": "●",     # ●
    "needs_you": "✋",   # ✋
    "done": "✓",        # ✓
    "failed": "✗",      # ✗
}

# Detail line for the cost-pause sentinel (kickoff rule 1). Exact
# wording is part of the user-facing contract — tests pin it.
COST_PAUSE_DETAIL = (
    "Paused: the daily budget was reached. "
    "/resume <run> continues it, /stop <run> ends it."
)
NEEDS_YOU_PREFIX = "Waiting for your answer: "
PUBLISHED_DETAIL = "Published"
WORKING_PREFIX = "Working: "
STARTING_DETAIL = "Starting"
FAILED_FALLBACK = "Failed"
FAILED_AT_PREFIX = "Failed at "
FAILED_AT_SUFFIX_OPEN = " ("
FAILED_AT_SUFFIX_CLOSE = ")"


@dataclass(frozen=True)
class TaskState:
    """The five fields the kickoff pins: state, detail, step, added, removed."""

    state: str
    detail: str
    step: str
    added: int
    removed: int


def _parse_payload(payload: Any) -> dict[str, Any]:
    """Defensive ``payload_json`` parser — the writer may store a dict or a JSON string.

    Mirrors the four call sites in ``agent.py`` (1472-1477, 1508-1514,
    1748-1755, 1973-1977). A parse failure yields ``{}`` so the
    downstream check (``human_questions``, ``finish_reason``) sees a
    missing key rather than crashing.
    """
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            data = __import__("json").loads(payload)
        except (ValueError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def _node_id_and_step(ev: dict[str, Any]) -> tuple[str, str]:
    """Best-effort ``(node_id, step)`` for one lifecycle event.

    The lifecycle event's payload carries the node id and node type;
    either may also live on the top-level row. Returns ``("", "")`` for
    an event that names neither, so the caller's fallback chain stays
    well-defined.
    """
    payload = _parse_payload(ev.get("payload_json"))
    node_id = str(
        payload.get("node_id") or ev.get("node_id") or ""
    )
    node_type = str(
        payload.get("node_type") or ev.get("node_type") or ""
    )
    step = node_id or node_type
    return node_id, step


def _current_step(events: list[dict[str, Any]]) -> str:
    """The latest ``node_start`` without a matching ``node_end``, else the last node seen.

    Kickoff §"Mechanism" rule 5: the running node is the LAST one
    whose ``node_start`` has not been followed by a ``node_end`` for
    the same id. Falls back to the last node seen at all when the
    lifecycle has only ends (an aborted run before any progress).
    """
    started: list[str] = []
    ended: set[str] = set()
    last_seen: str = ""
    for ev in events:
        node_id, step = _node_id_and_step(ev)
        if step:
            last_seen = step
        if ev.get("event_type") == "node_start" and node_id:
            started.append(node_id)
        elif ev.get("event_type") == "node_end" and node_id:
            ended.add(node_id)
    for node_id in reversed(started):
        if node_id not in ended:
            return node_id
    return last_seen


def _failing_node(events: list[dict[str, Any]]) -> tuple[str, str] | None:
    """The last ``node_end`` whose ``finish_reason`` is not ``"done"``; ``None`` when all clean.

    The detail format is ``"Failed at <id> (<finish_reason>)"`` when
    found, else plain ``"Failed"`` — kickoff rule 4. ``finish_reason``
    is a string key in the ``node_end`` payload; missing / empty is
    treated as ``"unknown"`` so the user still gets a useful line.
    """
    failing: tuple[str, str] | None = None
    for ev in events:
        if ev.get("event_type") != "node_end":
            continue
        node_id, _ = _node_id_and_step(ev)
        if not node_id:
            continue
        payload = _parse_payload(ev.get("payload_json"))
        reason = str(payload.get("finish_reason") or "unknown")
        if reason != "done":
            failing = (node_id, reason)
    return failing


def _diff_counts(run_dir: Path) -> tuple[int, int]:
    """``(added, removed)`` from the cached diff list; ``(0, 0)`` on any miss.

    ``cached_or_computed`` swallows every failure (no DB, no cache,
    no git) and returns ``(diffs, from_cache)``; the count loop is
    defensive against a per-file failure inside ``difflib``. Filters
    the ``+++``/``---`` headers so they do not count as content lines.
    """
    try:
        from mini_ork.acp.diffs import cached_or_computed

        diffs, _ = cached_or_computed(Path(run_dir))
    except Exception:  # noqa: BLE001 — count is best-effort
        return 0, 0
    added = 0
    removed = 0
    for entry in diffs:
        if not isinstance(entry, dict):
            continue
        old_text = entry.get("old_text") or ""
        new_text = entry.get("new_text") or ""
        try:
            for line in difflib.unified_diff(
                old_text.splitlines(), new_text.splitlines(), lineterm=""
            ):
                if line.startswith("+++") or line.startswith("---"):
                    continue
                if line.startswith("+"):
                    added += 1
                elif line.startswith("-"):
                    removed += 1
        except Exception:  # noqa: BLE001 — per-file failure is silent
            continue
    return added, removed


def task_state(run_dir: Path, snapshot: dict[str, Any]) -> TaskState:
    """Map a run snapshot to its ``TaskState`` per the five-rule kickoff spec.

    First match wins. The diff count is computed only for the
    ``done`` branch — every other state returns ``(0, 0)`` to keep
    the projection cheap. ``run_dir`` may be ``None``-typed at the
    call site; the ``Path`` cast in this function accepts both.
    """
    path = Path(run_dir) if run_dir is not None else None
    status = snapshot.get("status")
    events = list(snapshot.get("events") or [])

    # Rule 1: cost-pause sentinel wins over everything else (a paused
    # run may also be in a terminal state from a prior cycle).
    if path is not None and (path / ".cost-pause").exists():
        step = _current_step(events)
        return TaskState(
            state="needs_you",
            detail=COST_PAUSE_DETAIL,
            step=step,
            added=0,
            removed=0,
        )

    # Rule 2: an execute_blocked event with human_questions.
    for ev in events:
        if ev.get("event_type") != "execute_blocked":
            continue
        payload = _parse_payload(ev.get("payload_json"))
        questions = payload.get("human_questions")
        if isinstance(questions, list) and questions:
            first = questions[0]
            if isinstance(first, str) and first:
                step = _current_step(events)
                return TaskState(
                    state="needs_you",
                    detail=NEEDS_YOU_PREFIX + first,
                    step=step,
                    added=0,
                    removed=0,
                )

    # Rule 3: published → done.
    if status == "published":
        added, removed = (0, 0)
        if path is not None:
            added, removed = _diff_counts(path)
        return TaskState(
            state="done",
            detail=PUBLISHED_DETAIL,
            step=_current_step(events),
            added=added,
            removed=removed,
        )

    # Rule 4: failed / rolled_back → failed (with the failing node if any).
    if status in ("failed", "rolled_back"):
        failing = _failing_node(events)
        if failing is not None:
            node_id, reason = failing
            detail = (
                FAILED_AT_PREFIX
                + node_id
                + FAILED_AT_SUFFIX_OPEN
                + reason
                + FAILED_AT_SUFFIX_CLOSE
            )
        else:
            detail = FAILED_FALLBACK
        return TaskState(
            state="failed",
            detail=detail,
            step=_current_step(events),
            added=0,
            removed=0,
        )

    # Rule 5: anything else (None, classified, planned, executing, ...)
    # is working; the detail is the current step or "Starting".
    step = _current_step(events)
    detail = WORKING_PREFIX + step if step else STARTING_DETAIL
    return TaskState(
        state="working",
        detail=detail,
        step=step,
        added=0,
        removed=0,
    )


def run_mark(status: str | None, run_dir: Path | None) -> str:
    """Cheap mark glyph for ``list_sessions`` rows — no event or diff reads.

    A ``.cost-pause`` sentinel flips any non-terminal status to the
    "needs you" mark; published → done; failed / rolled_back → failed;
    anything else → working. The mark is the prefix Zed's thread list
    shows next to the run's title.
    """
    path = Path(run_dir) if run_dir is not None else None
    if path is not None and (path / ".cost-pause").exists() and status not in (
        "published",
        "rolled_back",
        "failed",
    ):
        return MARKS["needs_you"]
    if status == "published":
        return MARKS["done"]
    if status in ("failed", "rolled_back"):
        return MARKS["failed"]
    return MARKS["working"]


def title_with_state(base: str, ts: TaskState | None) -> str:
    """``base`` alone when no state; ``"<mark> <base>"`` otherwise.

    The diff suffix ``" +<added> −<removed>"`` (U+2212, NOT the
    ASCII ``-``) appends only when the state is ``done`` and at least
    one of ``added``/``removed`` is non-zero. ``base`` is the
    kickoff-derived title (or the thread's first-prompt title); the
    function never alters the text content, only prepends the mark.
    """
    if ts is None:
        return base
    out = f"{MARKS[ts.state]} {base}"
    if ts.state == "done" and (ts.added or ts.removed):
        out += f" +{ts.added} −{ts.removed}"
    return out


__all__ = ["MARKS", "TaskState", "task_state", "run_mark", "title_with_state"]
