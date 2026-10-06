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
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Persistent per-run diffstat cache. Distinct from ``diffs.CACHE_NAME``
# (``"acp-diffs.json"``), which is run-produced only. ``diffstat.json``
# holds the *aggregated* ``(added, removed)`` so the per-poll ``board
# --shell`` projection never re-runs ``git show`` for a terminal row
# whose counts were already computed. Schema is versioned (``"v": 1``)
# so a future format change degrades to a slow-path re-compute rather
# than a parse crash. Atomic via temp file + ``os.replace``; best-effort
# (OSError-swallowed) so a read-only home yields ``None`` instead of a
# board 500.
DIFFSTAT_NAME = "diffstat.json"
DIFFSTAT_VERSION = 1

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


def _diffstat_cached(run_dir: Path) -> tuple[int, int] | None:
    """``(added, removed)`` from the persisted diffstat cache; ``None`` on miss.

    Reads the per-run ``diffstat.json`` we write below. Three swallow
    cases — missing file, parse failure, wrong schema version — all
    degrade to a slow-path recompute in the caller. The ``v: 1`` gate
    lets a future writer bump the version and silently retire the old
    shape without a migration.
    """
    cache = Path(run_dir) / DIFFSTAT_NAME if run_dir is not None else None
    if cache is None or not cache.is_file():
        return None
    try:
        raw = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("v") != DIFFSTAT_VERSION:
        return None
    try:
        return int(raw.get("added", 0)), int(raw.get("removed", 0))
    except (TypeError, ValueError):
        return None


def _write_diffstat(run_dir: Path, added: int, removed: int) -> None:
    """Best-effort atomic write of ``diffstat.json`` for a terminal run.

    Per-writer temp file (``tempfile.mkstemp`` in the run dir) + ``os.replace``
    so a concurrent poll never reads a half-written cache AND two polls racing
    on the same run never collide on a shared ``diffstat.json.tmp`` name.
    ``dir=str(run_dir)`` keeps the rename atomic (same filesystem). ``OSError``
    swallowed so a read-only home degrades to "no cache" — the next poll falls
    through to the slow path. Never raises. Never writes for working /
    needs_you rows; the caller (``task_state`` rule 4) guards on the
    terminal-state branch.
    """
    target = Path(run_dir) / DIFFSTAT_NAME
    payload = {"added": int(added), "removed": int(removed), "v": DIFFSTAT_VERSION}
    fd: int | None = None
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(run_dir), prefix=".diffstat-", suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as h:
            h.write(json.dumps(payload))
        os.replace(tmp_path, target)
    except OSError:
        # Best-effort: drop the temp file if it survived, so the next poll
        # does not inherit a stale ``.diffstat-XXXX.tmp`` blob.
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _diff_counts(run_dir: Path) -> tuple[int, int, bool] | None:
    """``(added, removed, cacheable)`` from the cached diff list; ``None`` on hard failure.

    ``cacheable`` is True only when the counts can be safely persisted as
    ``diffstat.json``:

    - ``from_cache=True`` ⇒ the diff list came from the run's own
      ``acp-diffs.json`` (always non-empty when persisted, see
      ``diffs.run_diffs``'s ``if diffs and write_cache:`` gate). Cacheable.
    - ``from_cache=False and diffs`` ⇒ counts came from a live
      ``run_diffs`` call that returned a non-empty list. Cacheable.
    - ``from_cache=False and not diffs`` ⇒ ``run_diffs`` short-circuited
      to ``[]`` (no summary / worktree gone / empty ``files_changed``).
      This is the r4 cache-poisoning trap in disguise — *not* cacheable.
      An empty computed list must NOT persist ``(0, 0)``; the caller
      shows the row at ``(0, 0)`` for this poll and recomputes next poll
      (cheap: ``run_diffs`` returns before calling git).

    ``cached_or_computed`` swallows every failure (no DB, no cache,
    no git) and returns ``(diffs, from_cache)``; the count loop is
    defensive against a per-file failure inside ``difflib``. Filters
    the ``+++``/``---`` headers so they do not count as content lines.

    Returns ``None`` when the OUTER compute path (DB / git / cache
    parse) raises so the caller can leave the row at ``(0, 0)`` for
    this poll only and retry next poll — *never* persist a
    failure as ``(0, 0)`` (r5 fix 1, the r4 cache-poisoning trap).
    """
    try:
        from mini_ork.acp.diffs import cached_or_computed

        diffs, from_cache = cached_or_computed(Path(run_dir))
    except Exception:  # noqa: BLE001 — outer compute failure → no cache
        return None
    # ``from_cache`` ⇒ run-authored, always non-empty ⇒ cacheable.
    # ``from_cache=False`` ⇒ compute path: cacheable only if the live
    # ``run_diffs`` returned real diffs. Empty computed list is the r6 trap.
    cacheable = from_cache or bool(diffs)
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
    return added, removed, cacheable


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

    # Rule 3: terminal run with an open workspace record that still has
    # commits ahead or uncommitted changes → "ready to review". The
    # user must decide to merge or discard (Zed S5). Sits BEFORE the
    # published→done rule so the ✋ mark wins over ✓ when a run ends
    # with both a publish and a worktree to clean up.
    if status in ("published", "failed", "rolled_back"):
        ws_status = _workspace_status_for_terminal_run(path)
        if ws_status is not None:
            branch = ws_status["branch"]
            added = ws_status["added"]
            removed = ws_status["removed"]
            if status == "published":
                return TaskState(
                    state="needs_you",
                    detail=(
                        f"Ready to review: +{added} −{removed} on {branch} — "
                        "merge or discard it."
                    ),
                    step=_current_step(events),
                    added=added,
                    removed=removed,
                )
            # failed / rolled_back: stays failed; the kept-worktree note
            # is appended so the user knows /discard removes it.
            failing = _failing_node(events)
            if failing is not None:
                node_id, reason = failing
                base = (
                    FAILED_AT_PREFIX
                    + node_id
                    + FAILED_AT_SUFFIX_OPEN
                    + reason
                    + FAILED_AT_SUFFIX_CLOSE
                )
            else:
                base = FAILED_FALLBACK
            return TaskState(
                state="failed",
                detail=base + f" Its worktree is kept: /discard {path.name} removes it.",
                step=_current_step(events),
                added=0,
                removed=0,
            )

    # Rule 4: published → done.
    if status == "published":
        added, removed = (0, 0)
        if path is not None:
            # Read the persisted diffstat first — terminal rows never
            # change, so a hit spares us ``_diff_counts`` (which
            # funnels through ``cached_or_computed`` → ``run_diffs`` and
            # pays one ``git show`` per file on a cache miss). On a miss
            # we compute, then write the result so the next poll hits
            # the cache. Both the read and the write are guarded to
            # terminal rows — working/needs_you never write.
            cached_counts = _diffstat_cached(path)
            if cached_counts is not None:
                added, removed = cached_counts
            else:
                # Only cache a *successful* computation (r5 fix 1, r6
                # extension): a compute failure (None) OR a non-cacheable
                # result (empty computed list — the r4 cache-poisoning trap
                # reborn) leaves the row at (0, 0) for this poll only and
                # is retried next poll. ``cacheable`` is False when
                # ``run_diffs`` returned ``[]`` (missing summary, worktree
                # gone, empty ``files_changed``); the next poll pays one
                # ``git show`` to recover — cheap, no half-committed cache.
                computed = _diff_counts(path)
                if computed is not None:
                    added, removed, cacheable = computed
                    if cacheable:
                        _write_diffstat(path, added, removed)
        return TaskState(
            state="done",
            detail=PUBLISHED_DETAIL,
            step=_current_step(events),
            added=added,
            removed=removed,
        )

    # Rule 5: failed / rolled_back → failed (with the failing node if any).
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

    # Rule 6: anything else (None, classified, planned, executing, ...)
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


def _workspace_status_for_terminal_run(run_dir: Path | None) -> dict[str, Any] | None:
    """Workspace ``status(ws)``-shaped dict for a terminal run with an open
    workspace record whose branch still has commits ahead or uncommitted edits.

    ``None`` when the run dir has no parent home, no workspace record, or
    the worktree is already clean — those fall through to the published →
    done or failed rules. Defers the ``mini_ork.workspaces`` import to keep
    this module's import graph free of the runtime sandbox.
    """
    if run_dir is None:
        return None
    # run_dir = <home>/runs/<run_id> → home is parent.parent.
    home = run_dir.parent.parent
    run_id = run_dir.name
    try:
        from mini_ork import workspaces
    except Exception:  # noqa: BLE001 — best-effort, fall through
        return None
    try:
        ws = workspaces.load(home, run_id)
    except Exception:  # noqa: BLE001
        return None
    if ws is None:
        return None
    try:
        snap = workspaces.status(ws)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(snap, dict) or snap.get("exists") is not True:
        return None
    commits_ahead = int(snap.get("commits_ahead") or 0)
    uncommitted = snap.get("uncommitted") or []
    if commits_ahead <= 0 and not uncommitted:
        return None
    return {
        "branch": ws.branch,
        "added": int(snap.get("added") or 0),
        "removed": int(snap.get("removed") or 0),
    }


def run_mark(status: str | None, run_dir: Path | None) -> str:
    """Cheap mark glyph for ``list_sessions`` rows — no event or diff reads.

    A ``.cost-pause`` sentinel flips any non-terminal status to the
    "needs you" mark; a terminal run with an open workspace record
    whose branch still has changes also flips to ✋ (S5); published →
    done; failed / rolled_back → failed; anything else → working. The
    mark is the prefix Zed's thread list shows next to the run's title.
    """
    path = Path(run_dir) if run_dir is not None else None
    if path is not None and (path / ".cost-pause").exists() and status not in (
        "published",
        "rolled_back",
        "failed",
    ):
        return MARKS["needs_you"]
    if status in ("published", "failed", "rolled_back") and path is not None:
        home = path.parent.parent
        if (home / "worktrees" / f"{path.name}.json").is_file():
            return MARKS["needs_you"]
    if status == "published":
        return MARKS["done"]
    if status in ("failed", "rolled_back"):
        return MARKS["failed"]
    return MARKS["working"]


def title_with_state(base: str, ts: TaskState | None) -> str:
    """``base`` alone when no state; ``"<mark> <base>"`` otherwise.

    Two diff suffixes:

    * ``done`` → ``" +<added> −<removed>"`` (U+2212, NOT ASCII ``-``).
    * ``needs_you`` from a ready-to-review branch → ``" — ready to
      review +<added> −<removed>"`` (S5).

    ``base`` is the kickoff-derived title (or the thread's first-prompt
    title); the function never alters the text content, only prepends
    the mark and appends the suffix.
    """
    if ts is None:
        return base
    out = f"{MARKS[ts.state]} {base}"
    if ts.state == "done" and (ts.added or ts.removed):
        out += f" +{ts.added} −{ts.removed}"
    elif ts.state == "needs_you" and (ts.added or ts.removed) and ts.detail.startswith(
        "Ready to review:"
    ):
        out += f" — ready to review +{ts.added} −{ts.removed}"
    return out


__all__ = [
    "MARKS",
    "TaskState",
    "task_state",
    "run_mark",
    "title_with_state",
]
