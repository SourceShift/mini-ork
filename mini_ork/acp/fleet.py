"""Per-run fleet rows + run-card markdown for the ACP fleet surface (Zed S2).

``/runs`` answers "what is in flight and what is queued behind it"; ``/status
<run>`` answers "what happened and why". ``fleet_rows`` and ``run_card`` are
the read-model projections; ``render_fleet`` and ``render_card`` are the
markdown formatters. Together they replace the S1+Z1 ``handle_runs`` /
``handle_status`` shape with a fleet table + a run card.

Pure functions, no async, no ACP types, no module-level state — same
shape as ``mini_ork.acp.task_state`` (``task_state.py:11-12``: "Pure
functions, no async, no ACP types, no module-level state") and
``mini_ork.acp.diffs`` (``diffs.py:15-16``). ``FleetRow`` is
``frozen=True`` so callers can hash and compare cheaply and tests can
pin exact equality.
"""
from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mini_ork.acp.diffs import CACHE_NAME, cached_or_computed
from mini_ork.acp.history import list_runs
from mini_ork.acp.task_state import MARKS, run_mark, task_state

# Filter tokens the user may type on the ``/runs`` command line. The
# kickoff explicitly accepts ``needs_you`` and the hyphen variant
# ``needs-you``; internal comparison normalizes to the underscore form.
FILTER_STATES: tuple[str, ...] = ("all", "working", "needs_you", "needs-you", "done", "failed")
INTERNAL_STATES: tuple[str, ...] = ("all", "working", "needs_you", "done", "failed")
STATE_LABELS: dict[str, str] = {
    "working": "Working",
    "needs_you": "Needs you",
    "done": "Done",
    "failed": "Failed",
    "all": "All",
}

# Per-run node-start / node-end event types (lifecycle). Mirrors the
# S1 column list in ``repositories.fetch_node_lifecycle_events``.
NODE_LIFECYCLE = ("node_start", "node_end")

# Feature-name prefixes that the cost-by-stage grouping collapses onto
# a single label. ``mini-ork:<name>`` is the canonical llm_calls feature
# string; the renderer strips the prefix before lookup.
LEARNING_FEATURES = {"gradient-extract", "pattern-induct", "reflect"}
PROFILING_FEATURES = {"profile_answerer"}

# Cap on candidates fed through the cheap ``run_mark`` sweep before
# precise state is computed. Matches the kickoff's "candidate runs"
# budget for ``/runs``; ``history.list_runs`` is called with this value.
CANDIDATE_LIMIT = 200
# Default and hard cap for the ``/runs`` ``n`` arg (kickoff line 76).
DEFAULT_LIMIT = 20
MAX_LIMIT = 50


@dataclass(frozen=True)
class FleetRow:
    """One row in the ``/runs`` table — see kickoff ``fleet.py`` §"""

    run_id: str
    title: str
    recipe: str
    state: str
    mark: str
    step: str
    started_at: int | None
    ended_at: int | None
    cost_usd: float
    added: int
    removed: int


def _run_dir(home: Path, run_id: str) -> Path:
    """``<home>/runs/<run_id>`` — the canonical run directory path."""
    return Path(home) / "runs" / run_id


def _iso_to_epoch(value: Any) -> int | None:
    """Normalize an ISO-8601 UTC string OR a raw epoch int to epoch seconds.

    ``task_runs.created_at`` / ``updated_at`` are INTEGER unix timestamps in
    storage (migration 0013); ``list_runs`` normalizes them to ISO strings on
    read, while ``RunDetailRepository.fetch_task_run_row`` returns the raw
    ints. ``fleet_rows`` (ISO) and ``run_card`` (raw int) both funnel through
    this helper. Returns ``None`` for ``None`` / empty / unparseable text —
    the same defensive shape as ``history._normalize_ts``.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _parse_payload(payload: Any) -> dict[str, Any]:
    """Defensive ``payload_json`` parser — mirrors ``task_state._parse_payload``.

    ``run_events.payload_json`` may be a dict or a JSON string depending on
    the writer; a parse failure yields ``{}`` so the downstream key check
    (``node_id``, ``finish_reason``) sees a missing key rather than crashing.
    """
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def _stage_label(feature_name: str) -> str:
    """Map a ``mini-ork:<feature>`` name to the user-visible cost-group label.

    Per the kickoff: ``gradient-extract``/``pattern-induct``/``reflect`` →
    ``"learning"``; ``profile_answerer`` → ``"profiling"``. Other names pass
    through with their prefix stripped so the renderer never collapses an
    unmapped stage onto a misleading bucket.
    """
    name = feature_name.split(":", 1)[-1] if feature_name else ""
    if name in LEARNING_FEATURES:
        return "learning"
    if name in PROFILING_FEATURES:
        return "profiling"
    return name or "other"


def _format_duration(seconds: int) -> str:
    """Compact ``MmSs`` / ``HhMm`` / ``Dd`` rendering.

    Kickoff examples: ``"3m12s"`` for working rows, ``"1h04m"`` for long
    runs, ``"2d ago"`` for finished runs older than a day. Negative input
    clamps to ``0`` so a clock drift never prints ``"-3s"``.
    """
    if seconds < 0:
        seconds = 0
    if seconds >= 86400:
        return f"{seconds // 86400}d"
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{secs:02d}s"


def _diff_counts(run_dir: Path) -> tuple[int, int]:
    """``(added, removed)`` from the cached diff list; ``(0, 0)`` on any miss.

    Reuses ``task_state._diff_counts``'s contract: only the ``+``/``-``
    content lines count, ``+++``/``---`` headers are filtered, and a per-file
    failure is silent. We do NOT call ``cached_or_computed`` directly here —
    callers that need ``from_cache`` should call it once and pass the bool.
    """
    try:
        diffs, _ = cached_or_computed(run_dir)
    except Exception:  # noqa: BLE001 — best-effort
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
        except Exception:  # noqa: BLE001
            continue
    return added, removed


def _diff_counts_cached(run_dir: Path) -> tuple[int, int] | None:
    """``(added, removed)`` from the cached diff list only — ``None`` when no cache.

    ``_diff_counts`` falls through to ``run_diffs`` on a miss, which costs a
    ``git show`` per file. On a 200-row ``board --json`` we already paid for
    the candidates; never pay for diffs we cannot render cheaply.
    """
    cache = run_dir / CACHE_NAME
    if not cache.is_file():
        return None
    try:
        raw = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    # The cache writer at ``diffs._write_cache`` dumps a bare list, NOT a dict
    # with a ``"diffs"`` key — so the old ``raw.get("diffs")`` always returned
    # ``None`` and the function fell through to ``_diff_counts`` for every run.
    # Accept both shapes (a stale dict-shaped cache from older versions still
    # degrades to "no cache" instead of crashing).
    if isinstance(raw, list):
        entries: list[Any] = raw
    elif isinstance(raw, dict):
        raw_entries = raw.get("diffs")
        entries = raw_entries if isinstance(raw_entries, list) else []
    else:
        entries = []
    if not entries:
        return None
    added = 0
    removed = 0
    for entry in entries:
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
        except Exception:  # noqa: BLE001
            continue
    return added, removed


def _events_by_run(home: Path, run_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """All node-lifecycle events for the listed ``run_ids`` in one ``IN`` query.

    ``read_snapshot`` fans out per run (``fetch_node_lifecycle_events`` is one
    query per run). On ``board --json`` we have ≤ 50 shown runs, so this saves
    ~ 50 SELECTs. Returns ``{run_id: events}``; missing rows are absent.
    """
    if not run_ids:
        return {}
    state_db = Path(home) / "state.db"
    if not state_db.is_file():
        return {}
    placeholders = ",".join("?" for _ in run_ids)
    sql = (f"SELECT run_id, event_type, payload_json, created_at "
           f"FROM run_events WHERE run_id IN ({placeholders}) "
           f"AND event_type IN ('node_start','node_end') "
           f"ORDER BY created_at ASC")
    out_map: dict[str, list[dict[str, Any]]] = {rid: [] for rid in run_ids}
    try:
        from mini_ork.web.deps import db_for
        rows = db_for(Path(home)).rows(sql, tuple(run_ids))
    except Exception:  # noqa: BLE001 — best-effort batch read
        return out_map
    for row in rows or []:
        rid = str(row.get("run_id") or "")
        if rid in out_map:
            out_map[rid].append(row)
    return out_map


def _file_changes(run_dir: Path) -> tuple[list[dict[str, Any]], bool]:
    """Per-file ``+``/``-`` line counts from the diff cache; ``(False)`` when live-computed.

    Mirrors the kickoff: ``files changed with per-file +/- from
    diffs.cached_or_computed (never writes a cache) and whether that came
    from the cache``. The boolean drives the "(as the files are now)"
    honesty note in the run card.
    """
    try:
        diffs, from_cache = cached_or_computed(run_dir)
    except Exception:  # noqa: BLE001
        return [], True
    out: list[dict[str, Any]] = []
    for entry in diffs:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path") or ""
        old_text = entry.get("old_text") or ""
        new_text = entry.get("new_text") or ""
        added = 0
        removed = 0
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
        except Exception:  # noqa: BLE001
            continue
        out.append({"path": path, "added": added, "removed": removed})
    return out, from_cache


def _cost_by_stage(home: Path, run_id: str) -> tuple[dict[str, float], float]:
    """``(stage → USD, total)`` over the run's llm_calls.

    Source of truth: ``RunDetailRepository.fetch_llm_calls_by_run_id``. Each
    row's ``feature_name`` (after stripping ``mini-ork:``) is bucketed via
    ``_stage_label``. Rows with no ``cost_usd`` default to ``0.0`` so a
    schema drift in the cost column never crashes the projection.
    """
    from mini_ork.web.deps import db_for
    from mini_ork.web.repositories import RunDetailRepository

    try:
        repo = RunDetailRepository(db_for(Path(home)))
        rows = repo.fetch_llm_calls_by_run_id(run_id)
    except Exception:  # noqa: BLE001 — projection is best-effort
        return {}, 0.0
    groups: dict[str, float] = {}
    total = 0.0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        cost = float(row.get("cost_usd") or 0.0)
        total += cost
        label = _stage_label(str(row.get("feature_name") or ""))
        groups[label] = float(groups.get(label, 0.0)) + cost
    return groups, total


def _steps(home: Path, run_id: str) -> list[dict[str, Any]]:
    """Per-node step rows from ``run_events`` lifecycle events.

    Each step carries: ``node_id``, ``node_type``, ``lane`` (the ``actor``
    column from the lifecycle payload), ``start``, ``end``, ``duration``,
    ``finish_reason``, ``state`` (``done``/``running``/``failed``).
    A ``node_end`` with ``finish_reason`` not in ``("done", "")`` is
    ``failed`` — the same rule ``MiniOrkAcpAgent``'s ``_read_snapshot``
    consumers apply.
    """
    from mini_ork.web.deps import db_for
    from mini_ork.web.repositories import RunDetailRepository

    try:
        repo = RunDetailRepository(db_for(Path(home)))
        events = repo.fetch_node_lifecycle_events(run_id)
    except Exception:  # noqa: BLE001
        return []

    starts: dict[str, dict[str, Any]] = {}
    ends: dict[str, dict[str, Any]] = {}
    node_types: dict[str, str] = {}
    lanes: dict[str, str] = {}
    order: list[str] = []
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        kind = ev.get("event_type")
        if kind not in NODE_LIFECYCLE:
            continue
        payload = _parse_payload(ev.get("payload_json"))
        node_id = str(payload.get("node_id") or "")
        if not node_id:
            continue
        node_type = str(payload.get("node_type") or "")
        if node_id not in node_types and node_type:
            node_types[node_id] = node_type
        lane = str(payload.get("model_lane") or payload.get("lane") or "")
        if node_id not in lanes and lane:
            lanes[node_id] = lane
        ts = ev.get("created_at")
        if kind == "node_start":
            starts[node_id] = {"ts": ts, "payload": payload}
        elif kind == "node_end":
            ends[node_id] = {"ts": ts, "payload": payload}
        if node_id not in order:
            order.append(node_id)

    out: list[dict[str, Any]] = []
    for node_id in order:
        s = starts.get(node_id, {})
        e = ends.get(node_id)
        start_ts = s.get("ts") if isinstance(s, dict) else None
        end_ts = e.get("ts") if isinstance(e, dict) else None
        duration: int | None = None
        if isinstance(start_ts, (int, float)) and isinstance(end_ts, (int, float)):
            duration = int(end_ts) - int(start_ts)
        finish_reason = ""
        state = "running"
        if e is not None:
            payload = e.get("payload") if isinstance(e, dict) else {}
            finish_reason = str((payload or {}).get("finish_reason") or "")
            state = "failed" if finish_reason not in ("done", "") else "done"
        out.append(
            {
                "node_id": node_id,
                "node_type": node_types.get(node_id, ""),
                "lane": lanes.get(node_id, ""),
                "start": start_ts if isinstance(start_ts, (int, float)) else None,
                "end": end_ts if isinstance(end_ts, (int, float)) else None,
                "duration": duration,
                "finish_reason": finish_reason,
                "state": state,
            }
        )
    return out


def _verdict(run_dir: Path) -> dict[str, Any] | None:
    """``{verdict, reason}`` from ``<run_dir>/verdict.json``; ``None`` when missing.

    The recipe writes ``verdict.json`` end-to-end; the implementer never
    authors it. ``reason`` falls back to ``summary`` because the kickoff's
    wording ("its reason/summary field if present") leaves either name open.
    """
    path = Path(run_dir) / "verdict.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    verdict = raw.get("verdict")
    reason = raw.get("reason") or raw.get("summary") or None
    out: dict[str, Any] = {}
    if verdict is not None:
        out["verdict"] = verdict
    if reason is not None:
        out["reason"] = reason
    return out or None


def _learnings(home: Path, run_id: str, *, limit: int = 5) -> list[str]:
    """Titles from ``learning_record`` for ``run_id``, newest-first; ``[]`` on miss.

    Capped at ``limit`` so the card never buries the verdict line under
    dozens of titles. ``LearningRepository.fetch_learning_records`` orders
    by ``rank ASC, updated_at DESC`` — keep the cap on the consumer side.
    """
    from mini_ork.web.deps import db_for
    from mini_ork.web.repositories import LearningRepository

    try:
        repo = LearningRepository(db_for(Path(home)))
        rows = repo.fetch_learning_records(run_id)
    except Exception:  # noqa: BLE001
        return []
    titles: list[str] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        title = row.get("title")
        if isinstance(title, str) and title:
            titles.append(title)
        if len(titles) >= limit:
            break
    return titles


def fleet_rows(
    home: Path,
    *,
    state: str = "all",
    recipe: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[FleetRow], dict[str, int]]:
    """Build the ``/runs`` rows + state tab counts.

    Algorithm (kickoff lines 39-47):
      1. Pull ``CANDIDATE_LIMIT`` candidate rows from ``history.list_runs``.
      2. For each candidate, call ``run_mark`` (cheap) and bucket the
         candidate by its ``state`` (``working``/``needs_you``/``done``/
         ``failed``); the four buckets drive ``counts``.
      3. Apply ``state`` and ``recipe`` filters and slice to ``limit``.
      4. For each SHOWN row only, call ``history.read_snapshot`` +
         ``task_state`` + ``diffs.cached_or_computed`` (only for ``done``
         rows) to compute the precise ``state``/``step``/``added``/``removed``.
      5. Build ``FleetRow``s. ``started_at``/``ended_at`` are epoch ints;
         ``ended_at`` is set only for terminal states.

    The shape mirrors ``task_state.run_mark``'s "cheap then fat" posture so
    a 200-row project doesn't pay the I/O for every discarded candidate.
    """
    state_norm = state.replace("-", "_") if isinstance(state, str) else "all"
    if state_norm not in INTERNAL_STATES:
        state_norm = "all"
    bounded_limit = max(1, min(MAX_LIMIT, int(limit) if isinstance(limit, int) else DEFAULT_LIMIT))

    try:
        candidates, _ = list_runs(Path(home), limit=CANDIDATE_LIMIT, offset=0)
    except Exception:  # noqa: BLE001
        return [], {"working": 0, "needs_you": 0, "done": 0, "failed": 0}

    counts: dict[str, int] = {"working": 0, "needs_you": 0, "done": 0, "failed": 0}
    cheap_states: dict[str, str] = {}
    for row in candidates:
        run_id = row.get("run_id") or ""
        if not run_id:
            continue
        if recipe and (row.get("recipe") or "") != recipe:
            continue
        # Cost-pause sentinel flips any non-terminal status — match the
        # same rule ``task_state.run_mark`` applies (cheap, no DB).
        run_dir = _run_dir(home, run_id)
        try:
            mark = run_mark(row.get("status"), run_dir)
        except Exception:  # noqa: BLE001
            mark = MARKS.get("working", "●")
        # Reverse-map mark → state bucket; MARKS is one-to-one.
        cheap_state = "working"
        for k, v in MARKS.items():
            if v == mark:
                cheap_state = k
                break
        cheap_states[run_id] = cheap_state
        if cheap_state in counts:
            counts[cheap_state] += 1

    # Filter — state filter applies to the CHEAP bucket so the tab counts
    # already reflect "what's in this view", not "what's in the project".
    if state_norm != "all":
        filtered = [
            row for row in candidates
            if cheap_states.get(row.get("run_id") or "") == state_norm
        ]
    else:
        filtered = list(candidates)

    if recipe:
        filtered = [row for row in filtered if (row.get("recipe") or "") == recipe]

    shown = filtered[:bounded_limit]
    shown_run_ids = [str(r.get("run_id") or "") for r in shown if r.get("run_id")]
    events_by_run = _events_by_run(Path(home), shown_run_ids)

    rows: list[FleetRow] = []
    for row in shown:
        run_id = row.get("run_id") or ""
        recipe_name = str(row.get("recipe") or "")
        title = str(row.get("title") or "")
        cost = float(row.get("cost_usd") or 0.0)
        started_at = _iso_to_epoch(row.get("created_at"))
        updated_iso = row.get("updated_at")
        ended_at = _iso_to_epoch(updated_iso)

        run_dir = _run_dir(home, run_id)
        # Build the snapshot fleet_rows needs (status + events) from the list_runs
        # row and the batched events lookup — llm_calls is unused here, so we
        # avoid ``read_snapshot``'s 2 extra queries per row (≤ 50 shown).
        snapshot: dict[str, Any] = {
            "status": row.get("status"),
            "events": events_by_run.get(run_id, []),
            "llm_calls": [],
        }

        ts = task_state(run_dir, snapshot)
        precise_state = ts.state
        step = ts.step
        added = 0
        removed = 0
        if precise_state == "done":
            # ``task_state`` rule 4 (``mini_ork/acp/task_state.py:267-278``)
            # already called ``_diff_counts(path)`` for ``published`` rows and
            # stored the counts on ``TaskState.added/removed``. Recomputing
            # here paid for one ``git show`` per file via
            # ``cached_or_computed`` → ``run_diffs(write_cache=False)`` on
            # cold-cache rows — that was the dominant cost of ``board
            # --shell`` (204 subprocesses per poll on the researcher home).
            # Reuse the values already in hand. Fall back to ``_diff_counts``
            # only when the task_state run returned zeros AND no
            # ``acp-diffs.json`` exists — the one shape where the helper's
            # exception swallow could mask a non-zero result that the
            # page-build path's ``run_diffs(write_cache=True)`` had not yet
            # written. ``_diff_counts_cached`` and ``run_diffs`` both pass
            # ``write_cache=False``, so they never populate the cache.
            added, removed = ts.added, ts.removed
            if added == 0 and removed == 0 and not (run_dir / CACHE_NAME).is_file():
                added, removed = _diff_counts(run_dir)

        mark = MARKS.get(precise_state, MARKS["working"])

        # ``ended_at`` only for done/failed; for working/needs_you it stays None
        # so the renderer can show "elapsed" instead of "duration".
        if precise_state not in ("done", "failed"):
            ended_at = None

        rows.append(
            FleetRow(
                run_id=run_id,
                title=title,
                recipe=recipe_name,
                state=precise_state,
                mark=mark,
                step=step,
                started_at=started_at,
                ended_at=ended_at,
                cost_usd=cost,
                added=added,
                removed=removed,
            )
        )

    return rows, counts


def render_fleet(rows: list[FleetRow], counts: dict[str, int], *, state: str, now: int) -> str:
    """Format the ``/runs`` markdown — tabs, table, footer.

    First line is the tab row (``**All** 41 · Working 2 · ...``), active
    filter bolded. The table columns are ``| | run | recipe | step | time
    | cost | change |`` with a blank gutter column for the mark glyph.
    Empty result → ``"No runs match."``. Last line is the filter hint
    (verbatim per the kickoff).
    """
    active = state.replace("-", "_") if isinstance(state, str) else "all"
    if active not in INTERNAL_STATES:
        active = "all"

    parts: list[str] = []
    counts_total = sum(int(v) for v in counts.values())
    tab_specs = [
        ("all", "All", counts_total),
        ("working", "Working", int(counts.get("working", 0))),
        ("needs_you", "Needs you", int(counts.get("needs_you", 0))),
        ("done", "Done", int(counts.get("done", 0))),
        ("failed", "Failed", int(counts.get("failed", 0))),
    ]
    tab_line_parts: list[str] = []
    for key, label, count in tab_specs:
        if key == active:
            # Kickoff example: ``**All** 41`` — the LABEL is bold, the count
            # is not, so the bold never swallows the number.
            text = f"**{label}** {count}"
        else:
            text = f"{label} {count}"
        tab_line_parts.append(text)
    parts.append(" · ".join(tab_line_parts))

    if not rows:
        parts.append("")
        parts.append("No runs match.")
    else:
        headers = ("", "run", "recipe", "step", "time", "cost", "change")
        parts.append("")
        parts.append("| " + " | ".join(headers) + " |")
        parts.append("| " + " | ".join("---" for _ in headers) + " |")
        for r in rows:
            mark = r.mark
            title = (r.title or "").replace("|", "\\|")
            if len(title) > 48:
                title = title[:47] + "…"
            run_cell = f"{title} `{r.run_id}`"
            recipe_cell = r.recipe or ""
            if r.state in ("working", "needs_you"):
                step_cell = r.step or "—"
            else:
                step_cell = "—"
            # time formatting: elapsed for working/needs_you, duration for
            # done/failed, plus age for finished rows older than a day.
            if r.state in ("working", "needs_you") and r.started_at is not None:
                elapsed = max(0, int(now) - int(r.started_at))
                time_cell = _format_duration(elapsed)
            elif r.ended_at is not None and r.started_at is not None:
                duration = max(0, int(r.ended_at) - int(r.started_at))
                time_cell = _format_duration(duration)
                if r.ended_at is not None:
                    age = max(0, int(now) - int(r.ended_at))
                    if age >= 86400:
                        time_cell = f"{time_cell} · {_format_duration(age)} ago"
            else:
                time_cell = "—"
            cost_cell = f"${r.cost_usd:.2f}"
            if r.added or r.removed:
                # U+2212 unicode minus, NOT ASCII "-"
                change_cell = f"+{r.added} −{r.removed}"
            else:
                change_cell = "—"
            parts.append(
                "| "
                + " | ".join(
                    [
                        mark,
                        run_cell,
                        recipe_cell,
                        step_cell,
                        time_cell,
                        cost_cell,
                        change_cell,
                    ]
                )
                + " |"
            )

    parts.append("")
    parts.append(
        "Filter: `/runs working` · `/runs needs-you` · `/runs done` · "
        "`/runs failed` · `/runs recipe:<id>` · details: `/status <run id>`"
    )
    return "\n".join(parts)


def run_card(home: Path, run_id: str) -> dict[str, Any] | None:
    """Project a run's read-model state into a dict the renderer can shape.

    Returns ``None`` for an unknown ``run_id`` (no ``task_runs`` row).
    All exceptions are swallowed at the I/O boundaries so a missing DB or
    table degrades to ``{...with empty fields...}`` instead of a 500 — the
    same shape every other projection in the codebase uses.

    Fields:
      * ``title``, ``recipe``, ``status`` — raw from the run row + snapshot.
      * ``task_state`` — ``{"state", "step"}`` (the ``detail`` line lives
        only on the card; the row itself stays compact).
      * ``times`` — ``{"created", "updated"}`` as epoch ints (``None`` when
        missing).
      * ``steps`` — per-node ``{node_id, node_type, lane, start, end,
        duration, finish_reason, state}``.
      * ``cost_by_stage`` — ``{label: USD}``; ``cost_total`` is the sum.
      * ``files`` — ``[{path, added, removed}]``; ``files_from_cache``
        flags the honesty note.
      * ``verdict`` — ``{verdict, reason}`` or ``None``.
      * ``learnings`` — titles (≤ 5).
    """
    from mini_ork.web.deps import db_for
    from mini_ork.web.repositories import RunDetailRepository

    home = Path(home)
    run_id = str(run_id or "")
    if not run_id:
        return None
    try:
        repo = RunDetailRepository(db_for(home))
        row = repo.fetch_task_run_row(run_id)
    except Exception:  # noqa: BLE001
        return None
    if not row:
        return None

    from mini_ork.acp import history as _history

    try:
        snapshot = _history.read_snapshot(home, run_id)
    except Exception:  # noqa: BLE001
        snapshot = {"status": row.get("status"), "events": [], "llm_calls": []}

    run_dir = _run_dir(home, run_id)
    ts = task_state(run_dir, snapshot)

    files, files_from_cache = _file_changes(run_dir)
    cost_by_stage, cost_total = _cost_by_stage(home, run_id)
    verdict = _verdict(run_dir)
    learnings = _learnings(home, run_id)
    steps = _steps(home, run_id)

    recipe = str(row.get("recipe") or "")
    # ``task_runs`` has no title column — derive it from the kickoff's first
    # line exactly as ``history.list_runs`` does for the thread list, so the
    # card title matches the S1 thread title. Fall back to ``<recipe> run``
    # when the kickoff cannot be read.
    from mini_ork.acp.history import kickoff_text

    title = ""
    try:
        for line in kickoff_text(home, run_id, max_chars=2000).splitlines():
            stripped = line.strip()
            if stripped:
                title = stripped.lstrip("#").strip()[:80]
                break
    except Exception:  # noqa: BLE001 — title is best-effort
        title = ""
    if not title:
        title = f"{recipe or 'mini-ork'} run"

    return {
        "run_id": run_id,
        "title": title,
        "recipe": recipe,
        "status": snapshot.get("status"),
        "task_state": {"state": ts.state, "step": ts.step},
        "detail": ts.detail,
        "mark": MARKS.get(ts.state, MARKS["working"]),
        "times": {
            "created": _iso_to_epoch(row.get("created_at")),
            "updated": _iso_to_epoch(row.get("updated_at")),
        },
        "steps": steps,
        "cost_by_stage": cost_by_stage,
        "cost_total": cost_total,
        "files": files,
        "files_from_cache": files_from_cache,
        "verdict": verdict,
        "learnings": learnings,
    }


def render_card(card: dict[str, Any], *, now: int, serve_url: str | None) -> str:
    """Format the ``/status`` run card markdown.

    Order: title (with mark), state detail line, facts line, steps table,
    cost-by-stage line, files changed list, verdict, learnings, and the
    serve forensics URL when ``serve_url`` is set.
    """
    parts: list[str] = []
    mark = card.get("mark") or ""
    title = card.get("title") or ""
    parts.append(f"### {mark} {title}".strip())

    detail = card.get("detail") or ""
    if detail:
        parts.append("")
        parts.append(detail)

    recipe = card.get("recipe") or "—"
    times = card.get("times") or {}
    created = times.get("created")
    updated = times.get("updated")
    cost_total = float(card.get("cost_total") or 0.0)
    ts_state = (card.get("task_state") or {}).get("state") or "working"

    duration_text = "—"
    if isinstance(created, int):
        if ts_state in ("done", "failed") and isinstance(updated, int):
            duration_text = _format_duration(max(0, int(updated) - int(created)))
        else:
            duration_text = _format_duration(max(0, int(now) - int(created)))

    started_text = "—"
    if isinstance(created, int):
        try:
            started_text = datetime.fromtimestamp(int(created), tz=timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
        except (OverflowError, OSError, ValueError):
            started_text = "—"

    parts.append("")
    parts.append(
        f"recipe `{recipe}` · started {started_text} · {duration_text} · ${cost_total:.2f}"
    )

    steps = card.get("steps") or []
    if steps:
        parts.append("")
        parts.append("| step | type | model lane | time | result |")
        parts.append("| --- | --- | --- | --- | --- |")
        for step in steps:
            node_id = step.get("node_id") or ""
            node_type = step.get("node_type") or ""
            lane = step.get("lane") or "—"
            duration = step.get("duration")
            time_cell = _format_duration(int(duration)) if isinstance(duration, int) else "—"
            result = step.get("state") or "running"
            finish_reason = step.get("finish_reason") or ""
            if finish_reason and finish_reason != "done":
                result = f"{result} ({finish_reason})"
            parts.append(
                f"| `{node_id}` | {node_type} | `{lane}` | {time_cell} | {result} |"
            )

    cost_by_stage = card.get("cost_by_stage") or {}
    if cost_by_stage:
        parts.append("")
        rendered = " · ".join(
            f"{label} ${float(amount):.2f}"
            for label, amount in sorted(cost_by_stage.items())
        )
        parts.append(f"Cost by stage — {rendered}")

    files = card.get("files") or []
    if files:
        parts.append("")
        parts.append("Files changed:")
        for entry in files:
            path = entry.get("path") or ""
            added = int(entry.get("added") or 0)
            removed = int(entry.get("removed") or 0)
            from_cache = card.get("files_from_cache", True)
            if not from_cache and added == 0 and removed == 0:
                # Recomputed now and identical to the baseline: the run's change is
                # no longer in the working tree (reverted, or committed elsewhere).
                parts.append(f"- `{path}` (no difference now)")
            else:
                # U+2212 unicode minus, NOT ASCII "-"
                parts.append(f"- `{path}` +{added} −{removed}")
        if not card.get("files_from_cache", True):
            parts.append("")
            parts.append("_(compared with the files as they are now — no recorded diff for this run)_")

    verdict = card.get("verdict")
    if verdict:
        parts.append("")
        verdict_label = verdict.get("verdict") or "?"
        reason = verdict.get("reason") or ""
        line = f"Verdict: **{verdict_label}**"
        if reason:
            line += f" — {reason}"
        parts.append(line)

    learnings = card.get("learnings") or []
    if learnings:
        parts.append("")
        parts.append("Learnings:")
        for title in learnings:
            parts.append(f"- {title}")

    if serve_url:
        parts.append("")
        parts.append(f"Full forensics: {serve_url}/runs/{card.get('run_id') or ''}")

    return "\n".join(parts).rstrip()


__all__ = [
    "FleetRow",
    "fleet_rows",
    "render_fleet",
    "run_card",
    "render_card",
]