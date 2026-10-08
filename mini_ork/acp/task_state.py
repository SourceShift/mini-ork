"""Per-run task state for the ACP thread surface (Zed S1).

A single ``task_state(run_dir, snapshot)`` call answers the kickoff's
rule question — *what is this run doing right now, and is the user
needed?* — with no I/O beyond three cheap sentinels (``.cost-pause``,
``retry-gate.json``, ``landed.json``), the cached diff list and the
run's small level report (``verdict.json`` / ``run-verdict.json``);
every file read is skipped unless the run is terminal, and every read
fails soft. ``run_mark`` is the cheap sibling used by ``list_sessions``
for the run rows in the thread list — file reads only, never events or
diffs; ``title_with_state`` formats the title that lands in Zed's
thread list.

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
WITHHELD_PREFIX = "Not published: "
WITHHELD_SUFFIX = " unverified — review and decide"
LANDED_PREFIX = "Landed via "

# The level vocabulary and the run-dir files that carry it. Mirrors
# ``mini_ork.verify.levels`` (``LEVELS``/``PROVEN``/``REFUTED``/``NA``) and
# ``ide_pages.outcome``'s read order — kept local so this module stays a leaf
# (no import of the verify package).
LEVELS = ("applies", "executes", "target", "preserve", "contract")
PROVEN = "PROVEN"
REFUTED = "REFUTED"
NA = "n/a"
RUN_VERDICT_NAME = "run-verdict.json"
VERDICT_NAME = "verdict.json"
LANDED_NAME = "landed.json"


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


# ``finish_reason`` values a completed node carries. Anything else explicit
# ("error", "timeout", "interrupted", …) is a failure. ``levels_unverified`` is
# the publisher's own abstain signal (``publisher.py`` returns
# ``(0, "levels_unverified")``); it is a withheld publish, not a crashed node —
# treating it as a failure would blame the publisher and make every withheld
# surface decline.
_TERMINAL_OK_FINISH = ("done", "skipped", "abstain", "levels_unverified")

# Payload ``verdict`` values that say the node's own judgement was negative.
_FAIL_VERDICTS = (
    "request_changes", "escalate", "crash", "needs_revision", "fail", "failed",
)


def _node_end_failure(payload: dict[str, Any]) -> str | None:
    """The reason string when a ``node_end`` payload *says* it failed, else ``None``.

    Only an explicit failure signal counts (kickoff change 1):

    * a ``finish_reason`` other than ``done`` / ``skipped`` / ``abstain`` /
      ``levels_unverified``;
    * a ``verdict`` in ``{REQUEST_CHANGES, ESCALATE, CRASH, needs_revision,
      fail, failed}`` (case-insensitive);
    * a non-empty ``error``.

    A missing ``finish_reason`` with no other failure signal is NOT a failure.
    The live bug this rule closes: code-fix implementer ``node_end`` events
    carry no ``finish_reason``, so the old "missing ⇒ unknown ⇒ failed" rule
    blamed the implementer for a run that never failed at all.
    """
    reason = payload.get("finish_reason")
    if reason is not None and str(reason) != "":
        reason = str(reason)
        return None if reason in _TERMINAL_OK_FINISH else reason
    verdict = str(payload.get("verdict") or "")
    if verdict.lower() in _FAIL_VERDICTS:
        return verdict
    if str(payload.get("error") or ""):
        return "error"
    return None


def _failing_node(events: list[dict[str, Any]]) -> tuple[str, str] | None:
    """The last node still failing, as ``(node_id, reason)``; ``None`` when all clean.

    Per-node state: a node's *later* ``node_end`` (a revise round that passed)
    clears an earlier failure of the same id, so only the node whose LAST
    ``node_end`` carries a failure signal is reported. ``_node_end_failure``
    decides what "carries a failure signal" means — a bare ``node_end`` is not
    a failure. The detail format is ``"Failed at <id> (<reason>)"`` when found,
    else plain ``"Failed"`` (kickoff rule 4).
    """
    state: dict[str, str | None] = {}
    last_index: dict[str, int] = {}
    for index, ev in enumerate(events):
        if ev.get("event_type") != "node_end":
            continue
        node_id, _ = _node_id_and_step(ev)
        if not node_id:
            continue
        payload = _parse_payload(ev.get("payload_json"))
        state[node_id] = _node_end_failure(payload)
        last_index[node_id] = index
    failing = [nid for nid, reason in state.items() if reason is not None]
    if not failing:
        return None
    node_id = max(failing, key=lambda nid: last_index[nid])
    return node_id, str(state[node_id])


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


def _read_json_obj(path: Path) -> dict[str, Any] | None:
    """A JSON object from ``path``; ``None`` when missing, unparsable or not an object.

    The module's standing fail-soft contract: a half-written run dir, a
    read-only home or a banner line in front of the JSON degrades to ``None``
    rather than raising into a board 500.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def run_level_report(run_dir: Path | None) -> dict[str, Any] | None:
    """The run's level report: ``run-verdict.json``, else ``verdict.json`` when it
    carries ``levels`` / ``levels_decision``.

    Mirrors ``ide_pages.outcome._run_verdict`` (and ``retry_hint``'s read) so a
    run is judged withheld by exactly one rule everywhere. ``None`` when neither
    file exists or parses — a recipe that does not own ``verdict.json`` and a
    missing level vector both land here.
    """
    if run_dir is None:
        return None
    base = Path(run_dir)
    report = _read_json_obj(base / RUN_VERDICT_NAME)
    if report is not None:
        return report
    fallback = _read_json_obj(base / VERDICT_NAME)
    if fallback is not None and ("levels" in fallback or "levels_decision" in fallback):
        return fallback
    return None


def withheld_levels(run_dir: Path | None) -> tuple[list[str], list[str]] | None:
    """``(unproven, refuted)`` level names for a run whose publish was withheld.

    ``None`` when the run has no level report, or its ``levels_decision`` is not
    ``"abstain"`` — i.e. the publisher never withheld this run. ``unproven`` is
    every level that is neither ``PROVEN`` nor ``n/a`` (an ``n/a`` level has not
    failed), in report order; ``refuted`` is the subset that is explicitly
    ``REFUTED`` — a refute means the change itself was wrong, so callers route
    to the failed rule instead of "needs you".

    Read once, fail soft. Shared by ``task_state``, ``run_mark``,
    ``ide_pages.outcome`` and ``recovery.retry_hint`` so the cheap tile count
    and the precise list can never disagree about what "withheld" means.
    """
    report = run_level_report(run_dir)
    if not isinstance(report, dict):
        return None
    if str(report.get("levels_decision") or "") != "abstain":
        return None
    levels = report.get("levels")
    if not isinstance(levels, dict):
        return None
    unproven: list[str] = []
    refuted: list[str] = []
    for name in LEVELS:
        if name not in levels:
            continue
        value = str(levels.get(name) or "")
        if value in ("", PROVEN, NA):
            continue
        unproven.append(name)
        if value == REFUTED:
            refuted.append(name)
    if not unproven:
        return None
    return unproven, refuted


def withheld_publish(run_dir: Path | None,
                     events: list[dict[str, Any]] | None = None) -> list[str] | None:
    """The non-PROVEN level names when the publisher withheld and nothing failed.

    The single "needs you, not failed" gate — the withheld rule of ``task_state``
    (rule 2.5), ``run_mark``, ``ide_pages.outcome`` and ``recovery.retry_hint``
    all reach for this one function so the tile, the card and the retry hint can
    never disagree about whether a run is merely withheld.

    ``None`` (the run is NOT merely withheld) when:

    * the level report is missing or does not say ``abstain`` — the publisher
      never withheld it;
    * a level is explicitly ``REFUTED`` — the change itself is wrong, so the
      failed rule owns it (not a "needs you" decision);
    * ``events`` is given and a node failed per :func:`_failing_node` — a real
      failure, even when an earlier attempt's ``abstain`` verdict.json is still
      in the run dir (a recover re-run that died mid-node).

    ``events`` may be ``None`` for a caller that has not (or cannot) read the
    lifecycle: the level report alone then decides. That is the *provisional*
    answer ``run_mark`` takes before confirming against
    :func:`_lifecycle_events_for` — the cheap first pass keeps the file-only
    cost, so a caller that cannot see events must not present it as final. A
    caller that *can* see events but whose read failed (``retry_hint``) passes
    ``None`` only after deciding to decline — never as a silent "no node failed".
    """
    withheld = withheld_levels(run_dir)
    if withheld is None or withheld[1]:
        return None
    if events is not None and _failing_node(events) is not None:
        return None
    return withheld[0]


def landed_report(run_dir: Path | None) -> dict[str, Any] | None:
    """``landed.json`` for a terminal run whose change landed elsewhere.

    ``{"commit", "repo", "note"}`` (the sha kept whole; callers truncate to 9
    for display) or ``None`` when the file is missing / unparsable / carries no
    ``commit``. Written by the operator or a later tool — a delivered change
    must never keep looking failed.
    """
    if run_dir is None:
        return None
    data = _read_json_obj(Path(run_dir) / LANDED_NAME)
    if data is None:
        return None
    commit = str(data.get("commit") or "").strip()
    if not commit:
        return None
    return {
        "commit": commit,
        "repo": str(data.get("repo") or ""),
        "note": str(data.get("note") or ""),
    }


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

    # Rule -1 (landed): the change WAS delivered — by a later revision or a
    # direct commit — so a terminal run carrying ``landed.json`` is done, never
    # failed. First among the terminal rules: it beats the retry gate, the
    # withheld rule and the kept-worktree rule (kickoff §4). Cheap: only paid
    # when the row is terminal, and the read is a miss unless the file exists.
    if path is not None and status in ("failed", "rolled_back"):
        landed = landed_report(path)
        if landed is not None:
            detail = LANDED_PREFIX + landed["commit"][:9]
            if landed["note"]:
                detail += f" — {landed['note']}"
            return TaskState(
                state="done",
                detail=detail,
                step=_current_step(events),
                added=0,
                removed=0,
            )

    # Rule 0: a pending ``retry_precondition`` gate means a prior cycle
    # failed in a way the operator must fix before this run can continue.
    # Cheap: only paid when ``<run_dir>/retry-gate.json`` exists, and the
    # selector query is indexed on ``status='pending'``. Wins over the
    # cost-pause rule so the fix-step text stays visible alongside the
    # cost-pause detail.
    if path is not None and status in ("failed", "rolled_back"):
        try:
            from mini_ork.recovery import retry_notify
            home = path.parent.parent
            pending = retry_notify.pending_fix_for_run(home, path)
            if isinstance(pending, dict):
                ctxt_raw = pending.get("context")
                ctxt = ctxt_raw if isinstance(ctxt_raw, dict) else {}
                hint_raw = ctxt.get("hint")
                hint = hint_raw if isinstance(hint_raw, dict) else {}
                nc_raw = hint.get("needs_change")
                nc = nc_raw if isinstance(nc_raw, dict) else {}
                summary = str(nc.get("summary") or "")
                return TaskState(
                    state="needs_you",
                    detail=(
                        f"needs a fix: {summary[:60]}"
                        if summary else "needs a fix"
                    ),
                    step=_current_step(events),
                    added=0,
                    removed=0,
                )
        except Exception:  # noqa: BLE001
            pass

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

    # Rule 2.5 (withheld publish): every node passed but the publisher
    # abstained — a level was not PROVEN — so nothing failed and the user must
    # decide, not revise. Sits BEFORE the worktree rule so a kept worktree
    # cannot flip it back to "failed", and after the pending-gate / cost-pause
    # rules so a real blocker still wins. A level that is explicitly REFUTED, or
    # a node that actually failed (a stale abstain verdict.json from an earlier
    # attempt plus a recover re-run that died mid-node), falls through to the
    # failed rule — the change itself is wrong there.
    if path is not None and status in ("failed", "rolled_back"):
        withheld = withheld_publish(path, events)
        if withheld is not None:
            return TaskState(
                state="needs_you",
                detail=WITHHELD_PREFIX + ", ".join(withheld) + WITHHELD_SUFFIX,
                step=_current_step(events),
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


def _lifecycle_events_for(run_dir: Path) -> list[dict[str, Any]] | None:
    """A run's ``node_start`` / ``node_end`` rows, or ``None`` when unreadable.

    ``<home>/state.db`` is reached exactly as :func:`run_mark`'s other probes
    reach it (``run_dir.parent.parent``, the run id being the dir name). The
    imports are lazy and the guard broad so this stays a leaf: a missing, locked
    or schema-less DB degrades to ``None`` — the caller then keeps the
    level-report-only answer — instead of raising into a list render.
    """
    try:
        from mini_ork.web.db import db_for
        from mini_ork.web.repositories import RunDetailRepository

        rows = RunDetailRepository(db_for(run_dir.parent.parent)).fetch_node_lifecycle_events(
            run_dir.name)
    except Exception:  # noqa: BLE001 — no DB: no lifecycle
        return None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else None


def run_mark(status: str | None, run_dir: Path | None) -> str:
    """Cheap mark glyph for ``list_sessions`` rows — file reads, no diff reads.

    A ``.cost-pause`` sentinel flips any non-terminal status to the "needs you"
    mark; a terminal run that landed elsewhere (``landed.json``) is done; one
    whose publisher withheld (``verdict.json`` says ``abstain``, no level
    refuted, no failing node) is "needs you"; a terminal run with an open
    workspace record whose branch still changes also flips to ✋ (S5);
    published → done; failed / rolled_back → failed; anything else → working.
    The mark is the prefix Zed's thread list shows next to the run's title, so
    it must agree with ``task_state`` — both re-read the same helpers.

    The withheld branch is the one case the level report alone cannot settle: a
    stale ``abstain`` verdict.json over a run that actually died mid-node — a
    killed / reaped re-run whose crash end carries only ``verdict: CRASH`` —
    would read "needs you" here while the precise row says failed, and the
    kickoff requires the tile count and the list to agree. So this branch, and
    only this branch, confirms against the run's lifecycle with one bounded
    query (``_lifecycle_events_for``); every other mark stays a pure file read,
    and an unreadable DB keeps the level-report-only answer.
    """
    path = Path(run_dir) if run_dir is not None else None
    if path is not None and (path / ".cost-pause").exists() and status not in (
        "published",
        "rolled_back",
        "failed",
    ):
        return MARKS["needs_you"]
    if status in ("failed", "rolled_back") and path is not None:
        # Landed elsewhere → done (beats the worktree, gate and withheld rules),
        # then a withheld publish → needs_you. Both mirror ``task_state`` so the
        # tile count matches the list ("one count").
        if landed_report(path) is not None:
            return MARKS["done"]
        if withheld_publish(path) is not None:
            # Provisional (level report only) — confirm against the lifecycle so
            # a stale ``abstain`` over a crashed re-run marks failed, not
            # needs-you. An unreadable DB keeps the provisional answer.
            events = _lifecycle_events_for(path)
            if events is None or withheld_publish(path, events) is not None:
                return MARKS["needs_you"]
    if status in ("published", "failed", "rolled_back") and path is not None:
        home = path.parent.parent
        if (home / "worktrees" / f"{path.name}.json").is_file():
            return MARKS["needs_you"]
    if status in ("failed", "rolled_back") and path is not None:
        # A pending ``retry_precondition`` gate means the operator must act
        # before this run can continue — needs-you, not failed. Mirrors rule 0
        # of ``task_state`` so the cheap tile count agrees with the precise
        # list. Zero DB reads unless ``retry-gate.json`` exists.
        try:
            from mini_ork.recovery import retry_notify
            home = path.parent.parent
            if isinstance(retry_notify.pending_fix_for_run(home, path), dict):
                return MARKS["needs_you"]
        except Exception:  # noqa: BLE001
            pass
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
    "landed_report",
    "run_level_report",
    "run_mark",
    "task_state",
    "title_with_state",
    "withheld_levels",
    "withheld_publish",
]
