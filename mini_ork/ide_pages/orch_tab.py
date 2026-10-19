"""The run page's Orchestrator tab.

One place for the run's own supervisor, drawn like the node Stream tab: one
panel with a status line, kind filters, a scrollable transcript, and the
steering composer docked at its foot.

The supervisor is another agent, not a log. When a run was launched from an
ACP thread that thread *is* the orchestrator — its turns, thinking, tool calls
and replies are the live stream — and this tab reads that stream from the
thread's JSONL (``<home>/acp-threads/<thread_id>.jsonl``) in the same entry
grammar the node Stream uses. The composer then posts to the thread
(``threads send``), which steers the orchestrator's live turn; a message sent
after the run finished continues the thread.

A run launched without a thread (straight from the CLI) has no LLM
conversation to stream. It falls back to the watcher's own event log —
``orchestrator.*`` rows interleaved with the ``operator_steering`` rows that
go *into* the run — newest first, with the node-steering composer.

Every read here is passive: thread JSONL, ``run_events`` and
``operator_steering`` rows, the watcher's own state file, and the environment
knobs. Nothing writes, so the page can render a live run without racing its
watcher or its dispatcher.

Wiring lives in ``run.py`` (the tab registration and the page's poll
interval); this module owns only the tab's content so the two can be reviewed
apart.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from mini_ork.ide_pages import spec as S

# How many events the feed opens with. The orchestrator writes a handful of
# rows per run, so the window is mostly about a runner whose watcher has been
# signalling for hours; "show earlier" doubles it.
_DEFAULT_LIMIT = 200

# The feed's views. ``all`` is the default; the rest are the questions a
# reader actually asks of this log.
_VIEWS = (
    ("all", "all"),
    ("decision", "decisions"),
    ("signal", "signals"),
    ("steer", "steers"),
    ("lifecycle", "lifecycle"),
)

# The orchestrator stream's views. A conversation raises different questions
# from an event log: what was said, what tools ran, what the watcher flagged,
# and what the operator injected.
_STREAM_VIEWS = (
    ("all", "all"),
    ("talk", "conversation"),
    ("tools", "tools"),
    ("signals", "signals"),
    ("steers", "steers"),
)

# Caps for projecting a thread's JSONL into stream entries. A long-lived
# thread can hold thousands of records; the tab is a window on the current
# run's turn, not an archive.
_MAX_RECORDS = 5000
_MAX_ENTRY_TEXT = 20_000
_TOOL_LINE_CHARS = 300
_TOOL_LINES = 60
# A run is launched mid-turn: the user prompt that started it is the last
# ``user`` record at or before the run's start, with this much slack for the
# gap between the prompt and the dispatcher's first write.
_PROMPT_SLACK_S = 120

# One glyph and colour per event kind. The glyph carries the category at a
# glance; the colour is the row's left rule.
_MARKS: dict[str, tuple[str, str]] = {
    "advance": ("→", "blue"),
    "help": ("⚑", "green"),
    "repair": ("↻", "yellow"),
    "mutate": ("✎", "purple"),
    "promote": ("✦", "cyan"),
    "watching": ("●", "blue"),
    "signal": ("!", "orange"),
    "steered": ("⇒", "purple"),
    "ended": ("■", "sub"),
}

_SIGNAL_COLOURS = {
    "stalled": "yellow",
    "repeating_error": "orange",
    "duration_overrun": "orange",
    "cost_overrun": "red",
}

_SEVERITY_COLOURS = {"critical": "red", "warn": "yellow", "info": "sub"}


def _feed_item(glyph: str, text: str, detail: str = "", *, colour: str = "sub",
               text_colour: str = "text", when: str = "",
               do: dict[str, Any] | None = None,
               acts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """One feed row: ``glyph``+``colour`` are the event kind's mark."""
    out: dict[str, Any] = {"g": str(glyph), "t": str(text), "d": str(detail),
                           "c": str(colour), "tc": str(text_colour),
                           "when": str(when), "acts": list(acts or [])}
    if do is not None:
        out["do"] = do
    return out


def _feed_section(title: str, items: list[dict[str, Any]], *, status: str,
                  status_c: str, meta: str, filters: list[dict[str, Any]],
                  empty: str, copy: str, note: str = "",
                  footer: dict[str, Any] | None = None,
                  more: dict[str, Any] | None = None,
                  entries: list[dict[str, Any]] | None = None,
                  live: bool = False) -> dict[str, Any]:
    """The ``feed`` section the IDE draws: status line, filters, bounded rows.

    ``stream`` asks the IDE for the node-Stream layout: one panel with the
    status line, the filter chips, the transcript, and the steering composer
    docked at the bottom. The carded feed (no ``stream``) stays available for
    pages that only need a status board.

    Written here rather than in ``spec.py`` so this tab and its renderer can
    land together; the section shape is the payload contract, and the IDE
    treats an unknown ``type`` as an empty card.
    """
    section: dict[str, Any] = {
        "type": "feed", "title": title, "note": note, "actions": [], "full": True,
        "stream": True,
        "status": status, "status_c": status_c, "meta": meta,
        "items": items, "filters": filters, "empty": empty, "copy": copy,
    }
    if entries is not None:
        # The orchestrator's own transcript: node-Stream entries, oldest
        # first. The IDE draws these instead of `items` when present.
        section["entries"] = entries
        section["live"] = bool(live)
    if footer:
        section["footer"] = footer
    if more:
        section["more"] = more
    return section


def _clock(epoch: int | float | None) -> str:
    """Wall-clock time of day for a seconds stamp; ``—`` when there is none."""
    try:
        stamp = float(epoch or 0)
    except (TypeError, ValueError):
        return "—"
    if stamp <= 0:
        return "—"
    return time.strftime("%H:%M:%S", time.localtime(stamp))


def _ms_clock(epoch_ms: int | float | None) -> str:
    """The steering table stores milliseconds; the feed prints seconds."""
    try:
        stamp = float(epoch_ms or 0) / 1000.0
    except (TypeError, ValueError):
        return "—"
    return _clock(stamp)


def _limit(args: dict[str, Any] | None) -> int:
    raw = (args or {}).get("orch_limit")
    if raw is None:
        return _DEFAULT_LIMIT
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    # The page only ever asks for at least the default, but a test (and a
    # future "just this wave" view) may ask for fewer.
    return max(1, min(value, 5000))


def _view(args: dict[str, Any] | None) -> str:
    raw = str((args or {}).get("orch_view") or "all")
    known = {key for key, _ in _VIEWS} | {key for key, _ in _STREAM_VIEWS}
    return raw if raw in known else "all"


def _category(kind: str) -> str:
    if kind == "signal":
        return "signal"
    if kind == "steered":
        return "steer"
    if kind in ("watching", "ended"):
        return "lifecycle"
    return "decision"


def _payload(raw: Any) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _events(run: Any, limit: int) -> tuple[list[dict[str, Any]], bool]:
    """The newest ``limit`` orchestrator events, and whether older ones exist."""
    from mini_ork.web.db import db_for

    db = db_for(run.home)
    if not db.has_table("run_events"):
        return [], False
    rows = db.rows(
        "SELECT event_type, payload_json, created_at FROM run_events "
        "WHERE run_id = ? AND event_type LIKE 'orchestrator.%' "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (run.id, limit + 1),
    )
    return rows[:limit], len(rows) > limit


def _steering_rows(run: Any, limit: int = 8) -> list[dict[str, Any]]:
    from mini_ork.web.db import db_for

    db = db_for(run.home)
    if not db.has_table("operator_steering"):
        return []
    return db.rows(
        "SELECT id, role_target, severity, message, source, confidence, "
        "created_at, expires_at, consumed_at FROM operator_steering "
        "WHERE run_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
        (run.id, limit),
    )


def _running(run: Any) -> bool:
    return run.card.get("status") not in (
        "published", "failed", "rolled_back", "not_needed",
    )


def _budget_cap() -> float:
    raw = os.environ.get("MO_ORCH_BUDGET_USD", "") or "1.0"
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 1.0


def _watcher(run: Any) -> dict[str, Any]:
    """The watcher's state, read straight from its own state file.

    An import or read failure degrades to ``known=False`` rather than hiding
    the tab: the feed above it is still worth reading.
    """
    out: dict[str, Any] = {
        "known": False,
        "enabled": True,
        "watch_enabled": True,
        "active": False,
        "state": None,
        "config": None,
        "error": "",
    }
    try:
        from mini_ork.orchestration import runner_orchestrator as orch

        out["enabled"] = orch.enabled()
        out["watch_enabled"] = orch.watch_enabled()
        out["config"] = orch.WatchConfig.from_env()
        out["state"] = orch.load_watch_state(run.run_dir)
        out["active"] = bool(orch.watch_active(run.run_dir))
        out["known"] = True
    except Exception as exc:  # noqa: BLE001 — the tab must render without the watcher
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _status_line(run: Any, watcher: dict[str, Any]) -> tuple[str, str, str]:
    """``(status text, colour, meta)`` for the feed's header."""
    if not watcher["known"]:
        return "watcher unavailable", "red", watcher["error"]
    config = watcher["config"]
    state = watcher["state"]
    if not watcher["enabled"]:
        return "off", "dim", "MO_RUN_ORCHESTRATOR=0"
    if not watcher["watch_enabled"]:
        return "off", "dim", "MO_ORCH_WATCH=0"
    if config is not None and config.autonomy == "off":
        return "off", "dim", "MO_ORCH_WATCH_AUTONOMY=off"
    steers = getattr(state, "steers", 0)
    cap = getattr(config, "max_steers", 0)
    interval = getattr(config, "interval_s", 0)
    autonomy = getattr(config, "autonomy", "steer")
    # The watcher's own numbers are the tab's "anything else related": they
    # ride the stream's status line rather than a card of their own, so the
    # panel is one surface and not a dashboard of separate boxes.
    base_meta = f"autonomy {autonomy} · steers {steers}/{cap} · budget ${_budget_cap():g}"
    if watcher["active"]:
        meta = f"every {interval:g}s · {base_meta} · tick {getattr(state, 'tick_count', 0)}"
        return "watching", "green", meta
    if state is not None and getattr(state, "ended_reason", None):
        return f"stopped · {state.ended_reason}", "sub", base_meta
    if _running(run):
        return "armed", "blue", f"watcher has not ticked yet · every {interval:g}s · {base_meta}"
    return "stopped", "sub", base_meta


def _row(run: Any, raw: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    """One feed row plus its category and its copy-log line."""
    kind = str(raw.get("event_type") or "").removeprefix("orchestrator.")
    payload = _payload(raw.get("payload_json"))
    signal = str(payload.get("kind") or "")
    glyph, colour = _MARKS.get(kind, ("•", "sub"))
    if kind == "signal" and signal in _SIGNAL_COLOURS:
        colour = _SIGNAL_COLOURS[signal]
    label = kind if kind != "signal" or not signal else signal
    node = str(payload.get("node_id") or "")
    reason = str(payload.get("reason") or payload.get("detail") or "")
    detail = " · ".join(part for part in (node, reason) if part)
    when = _clock(raw.get("created_at"))
    acts: list[dict[str, Any]] = []
    do = None
    if node:
        do = S.page_link("run", run=run.id, node=node)
        acts = [S.btn("Open", do, "ghost")]
    item = _feed_item(
        glyph,
        label,
        detail,
        colour=colour,
        text_colour="text" if kind != "ended" else "sub",
        when=when,
        do=do,
        acts=acts,
    )
    line = " ".join(part for part in (when, label, detail) if part)
    return item, _category(kind), line


def _steering_items(run: Any) -> list[tuple[float, dict[str, Any], str]]:
    """Steering rows as stream entries: ``(epoch_s, item, copy_line)``.

    The queue used to be a card of its own. It is the steering side of the
    same story — what went *into* the run — so it belongs in the stream, under
    the ``steers`` chip, sorted into the same newest-first order.
    """
    now_ms = int(time.time() * 1000)
    out: list[tuple[float, dict[str, Any], str]] = []
    for row in _steering_rows(run):
        severity = str(row.get("severity") or "info")
        message = str(row.get("message") or "").strip()
        message = message if len(message) <= 160 else message[:157] + "…"
        target = str(row.get("role_target") or "any")
        source = str(row.get("source") or "operator")
        consumed = row.get("consumed_at")
        expires_ms = int(row.get("expires_at") or 0)
        if consumed:
            state, glyph, colour = "read", "✓", "sub"
        elif expires_ms and expires_ms < now_ms:
            state, glyph, colour = "expired", "·", "sub"
        else:
            state, glyph, colour = "queued", "⇒", _SEVERITY_COLOURS.get(severity, "sub")
        created_ms = float(row.get("created_at") or 0)
        item = _feed_item(
            glyph,
            message or "(empty steering message)",
            f"{target} · {source} · {state}",
            colour=colour,
            text_colour="text" if state != "expired" else "sub",
            when=_ms_clock(created_ms),
        )
        line = f"{_ms_clock(created_ms)} steer[{severity}/{state}] {target}: {message}"
        out.append((created_ms / 1000.0, item, line))
    return out


# ── the orchestrator's own stream ───────────────────────────────────────────


def _thread_id(run: Any) -> str:
    """The ACP thread that owns this run, or ``""`` when none does.

    Two persisted sources, cheapest first: ``owner.json`` (written at run
    start from ``MO_RUN_OWNER=thread:<id>``, see
    ``mini_ork.recovery.retry_notify.owner``) and the ``thread_runs`` join
    the threads service writes. A run launched straight from the CLI has
    neither — there is no orchestrator conversation to stream, only the
    watcher's event log.
    """
    try:
        from mini_ork.recovery import retry_notify

        record = retry_notify.owner(run.home, run.id)
    except Exception:  # noqa: BLE001 — a missing owner must not hide the tab
        record = None
    if isinstance(record, dict) and record.get("kind") == "thread":
        candidate = str(record.get("id") or "")
        if candidate:
            return candidate

    from mini_ork.web.db import db_for

    try:
        db = db_for(run.home)
        if not db.has_table("thread_runs"):
            return ""
        rows = db.rows(
            "SELECT thread_id FROM thread_runs WHERE run_id = ? "
            "ORDER BY attempt DESC, id DESC LIMIT 1",
            (run.id,),
        )
    except Exception:  # noqa: BLE001 — a missing DB is "no thread", not an error
        return ""
    return str(rows[0].get("thread_id") or "") if rows else ""


def _thread_records(home: Any, thread_id: str) -> list[dict[str, Any]]:
    """The thread's JSONL records, oldest first, capped to a recent window.

    A malformed id or an unreadable file degrades to no records: the tab
    still shows the watcher's log beside an empty transcript, which is a
    better failure than a page that will not render.
    """
    from mini_ork.acp.threads import ThreadStore

    try:
        records = ThreadStore(home).read(thread_id)
    except (OSError, ValueError):
        return []
    return records[-_MAX_RECORDS:]


def _thread_meta(records: list[dict[str, Any]]) -> tuple[str, str]:
    """The thread's lane and mode from its last ``config`` record."""
    lane, mode = "", ""
    for record in records:
        if record.get("type") == "config":
            lane = str(record.get("model") or lane)
            mode = str(record.get("mode") or mode)
    return lane, mode


def _started_at(run: Any) -> int:
    try:
        return int(run.row.get("created_at") or 0)
    except (TypeError, ValueError):
        return 0


def _chunk_text(update: dict[str, Any]) -> str:
    content = update.get("content")
    if isinstance(content, dict):
        return str(content.get("text") or "")
    if isinstance(content, str):
        return content
    return ""


def _tool_result_lines(update: dict[str, Any], failed: bool) -> list[dict[str, str]]:
    """A tool result's text as ``{t, c}`` lines, capped like the node Stream."""
    out: list[str] = []
    content = update.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            inner = block.get("content")
            text = inner.get("text") if isinstance(inner, dict) else block.get("text")
            if not isinstance(text, str):
                continue
            out.extend(text.splitlines())
    if not out:
        return []
    if len(out) > _TOOL_LINES:
        hidden = len(out) - _TOOL_LINES
        out = out[:_TOOL_LINES] + [f"… {hidden} more lines"]
    return [
        {"t": line[:_TOOL_LINE_CHARS], "c": "red" if failed else "body"}
        for line in out
    ]


def _thread_stream(
    records: list[dict[str, Any]], started: int,
) -> list[tuple[float, dict[str, Any], str, str]]:
    """The thread's records as node-Stream entries: ``(ts, entry, category, line)``.

    Kinds mirror ``node.py``: a user prompt is ``user``, the assistant's
    prose is ``text``, its reasoning ``think``, a tool call ``tool`` (with
    the result as ``lines``), and everything else a ``note``. Chunks of one
    message accumulate into a single entry so a transcript reads as messages
    rather than as a delta log.
    """
    # Start at the prompt that launched this run: the last user record at or
    # before the run's start. Earlier turns of the same thread belong to
    # other runs and would push this one's story off the window.
    start = 0
    if started > 0:
        for index, record in enumerate(records):
            if record.get("type") == "user" and float(record.get("t") or 0) <= started + _PROMPT_SLACK_S:
                start = index

    out: list[tuple[float, dict[str, Any], str, str]] = []
    tools: dict[str, int] = {}  # toolCallId → index into `out`
    current: tuple[str, int] | None = None  # (kind, index) of a chunk entry

    def push(ts: float, entry: dict[str, Any], category: str, line: str) -> int:
        out.append((ts, entry, category, line))
        return len(out) - 1

    def append_chunk(ts: float, kind: str, text: str) -> None:
        nonlocal current
        if not text:
            return
        if current is not None and current[0] == kind:
            entry = out[current[1]][1]
            merged = str(entry.get("arg") or "") + text
            entry["arg"] = merged[:_MAX_ENTRY_TEXT]
            out[current[1]] = (out[current[1]][0], entry, out[current[1]][2], out[current[1]][3])
            return
        entry: dict[str, Any] = {"k": kind, "head": "Thinking… " if kind == "think" else "",
                                 "arg": text[:_MAX_ENTRY_TEXT]}
        current = (kind, push(ts, entry, "talk", text))

    for record in records[start:]:
        stamp = float(record.get("t") or 0)
        rtype = record.get("type")
        if rtype == "user":
            text = str(record.get("text") or "").strip()
            current = None
            if text:
                entry = {"k": "user", "head": "", "arg": text[:_MAX_ENTRY_TEXT]}
                push(stamp, entry, "talk", text.splitlines()[0][:160])
            continue
        if rtype != "update":
            continue
        update = record.get("update")
        if not isinstance(update, dict):
            continue
        kind = str(update.get("sessionUpdate") or update.get("session_update") or "")
        if kind == "agent_message_chunk":
            append_chunk(stamp, "text", _chunk_text(update))
        elif kind == "agent_thought_chunk":
            append_chunk(stamp, "think", _chunk_text(update))
        elif kind == "tool_call":
            current = None
            title = str(update.get("title") or update.get("kind") or "tool")
            entry = {"k": "tool", "head": title[:_TOOL_LINE_CHARS], "arg": "", "lines": []}
            index = push(stamp, entry, "tools", title[:160])
            tool_id = str(update.get("toolCallId") or "")
            if tool_id:
                tools[tool_id] = index
        elif kind == "tool_call_update":
            index = tools.get(str(update.get("toolCallId") or ""))
            if index is None:
                continue
            status = str(update.get("status") or "")
            failed = status in ("failed", "error", "cancelled")
            lines = _tool_result_lines(update, failed)
            if lines:
                entry = out[index][1]
                existing = entry.get("lines")
                entry["lines"] = (existing if isinstance(existing, list) else []) + lines
                out[index] = (out[index][0], entry, out[index][2], out[index][3])
    return out


def _event_stream(rows: list[dict[str, Any]]) -> list[tuple[float, dict[str, Any], str, str]]:
    """Watcher events as stream entries — the no-thread run's transcript."""
    out: list[tuple[float, dict[str, Any], str, str]] = []
    for raw in rows:
        kind = str(raw.get("event_type") or "").removeprefix("orchestrator.")
        payload = _payload(raw.get("payload_json"))
        signal = str(payload.get("kind") or "")
        glyph = _MARKS.get(kind, ("•", "sub"))[0]
        label = kind if kind != "signal" or not signal else signal
        node = str(payload.get("node_id") or "")
        reason = str(payload.get("reason") or payload.get("detail") or "")
        detail = " · ".join(part for part in (node, reason) if part)
        text = f"{glyph} {label}{(' · ' + detail) if detail else ''}"
        stamp = float(raw.get("created_at") or 0)
        out.append((stamp, {"k": "note", "head": "", "arg": text}, "signals", text))
    return out


def _steer_stream(run: Any) -> list[tuple[float, dict[str, Any], str, str]]:
    """Steering rows as ``steer`` entries — what went into the run."""
    now_ms = int(time.time() * 1000)
    out: list[tuple[float, dict[str, Any], str, str]] = []
    try:
        rows = _steering_rows(run, limit=200)
    except Exception:  # noqa: BLE001 — the thread transcript must still render
        rows = []
    for row in rows:
        message = str(row.get("message") or "").strip()
        if not message:
            continue
        target = str(row.get("role_target") or "any")
        consumed = row.get("consumed_at")
        expires_ms = int(row.get("expires_at") or 0)
        if consumed:
            state = "read"
        elif expires_ms and expires_ms < now_ms:
            state = "expired"
        else:
            state = "queued"
        stamp = float(row.get("created_at") or 0) / 1000.0
        out.append((
            stamp,
            {"k": "steer", "head": "", "arg": message},
            "steers",
            f"{target} · {state}: {message.splitlines()[0][:160]}",
        ))
    return out


def _stream_footer(run: Any, thread_id: str) -> dict[str, Any]:
    """The composer docked under the orchestrator's stream.

    With a thread, the message goes to the orchestrator itself; a live turn
    is steered by it, and a finished run's thread is continued by it. Without
    one, there is no orchestrator to talk to, and the fallback steers the
    running node — said plainly so the input is not mistaken for a chat.
    """
    if thread_id:
        return {
            "placeholder": "Message the orchestrator… (Enter to send)",
            "cli": ["threads", "send", thread_id, "--text"],
            "note": (
                "⏵⏵ steers the orchestrator's live turn · after the run "
                "finishes the same message continues this thread"
            ),
        }
    if not _running(run):
        return {"note": "This run is finished — steering is consumed only by a live run."}
    return {
        "placeholder": "Steer the running node… (Enter to send)",
        "cli": ["board", "steer", run.id, "--role", "any", "--text"],
        "note": (
            "no orchestrator thread owns this run · ⏵⏵ lands as operator "
            "steering, consumed on the node's next turn"
        ),
    }


def _thread_feed(run: Any, args: dict[str, Any] | None, thread_id: str) -> dict[str, Any]:
    """The run's orchestrator as a live transcript, oldest first."""
    limit = _limit(args)
    view = _view(args)
    watcher = _watcher(run)
    status, status_c, meta = _status_line(run, watcher)
    records = _thread_records(run.home, thread_id)
    lane, mode = _thread_meta(records)
    if lane:
        meta = f"orchestrator {lane}{(' · ' + mode) if mode else ''} · {meta}"
    try:
        rows, events_more = _events(run, limit)
    except Exception:  # noqa: BLE001 — the thread transcript must still render
        rows, events_more = [], False

    merged = _thread_stream(records, _started_at(run))
    merged.extend(_event_stream(rows))
    merged.extend(_steer_stream(run))
    merged.sort(key=lambda part: part[0])

    counts = {key: 0 for key, _ in _STREAM_VIEWS}
    lines: list[str] = []
    kept: list[dict[str, Any]] = []
    for _, entry, category, line in merged:
        counts["all"] += 1
        counts[category] = counts.get(category, 0) + 1
        lines.append(line)
        if view in ("all", category):
            kept.append(entry)

    # The stream is a window on its newest entries, like the node Stream: a
    # long thread keeps the run's current turn on screen.
    more = len(kept) > limit or events_more
    if len(kept) > limit:
        kept = kept[-limit:]

    filters = [
        {
            "t": f"{label} {counts[key]}",
            "on": view == key,
            "do": S.set_args(orch_view=key),
        }
        for key, label in _STREAM_VIEWS
    ]
    empty = (
        "This run's orchestrator thread is empty — no turns have been recorded yet."
        if not counts["all"]
        else f"No {dict(_STREAM_VIEWS).get(view, view)} in the last {counts['all']} entries."
    )
    return _feed_section(
        "Orchestrator",
        [],
        status=status,
        status_c=status_c,
        meta=meta,
        filters=filters,
        empty=empty,
        copy="\n".join(lines),
        footer=_stream_footer(run, thread_id),
        more=(
            {
                "t": f"Show earlier entries (window: {limit})",
                "do": S.set_args(orch_limit=min(limit * 2, 5000)),
            }
            if more
            else None
        ),
        entries=kept,
        live=_running(run),
    )


def _feed(run: Any, args: dict[str, Any] | None) -> dict[str, Any]:
    thread_id = _thread_id(run)
    if thread_id:
        return _thread_feed(run, args, thread_id)
    return _event_feed(run, args)


def _event_feed(run: Any, args: dict[str, Any] | None) -> dict[str, Any]:
    limit = _limit(args)
    view = _view(args)
    watcher = _watcher(run)
    status, status_c, meta = _status_line(run, watcher)
    rows, more = _events(run, limit)

    # One stream, newest first: the orchestrator's own events and the steering
    # it (or the operator) queued are the same story told from both sides.
    # Interleaving them here is what lets the tab be a transcript rather than
    # a stack of cards that each answer a neighbouring question.
    merged: list[tuple[float, dict[str, Any], str, str]] = []
    for raw in rows:
        item, category, line = _row(run, raw)
        merged.append((float(raw.get("created_at") or 0), item, category, line))
    for stamp, item, line in _steering_items(run):
        merged.append((stamp, item, "steer", line))
    merged.sort(key=lambda part: part[0], reverse=True)

    items: list[dict[str, Any]] = []
    counts = {key: 0 for key, _ in _VIEWS}
    lines: list[str] = []
    for part in merged:
        item, category, line = part[1], part[2], part[3]
        counts["all"] += 1
        counts[category] += 1
        lines.append(line)
        if view in ("all", category):
            items.append(item)

    filters = [
        {
            "t": f"{label} {counts[key]}",
            "on": view == key,
            "do": S.set_args(orch_view=key),
        }
        for key, label in _VIEWS
    ]
    empty = (
        "No interventions — the orchestrator is quiet."
        if view == "all"
        else f"No {dict(_VIEWS)[view]} in the last {counts['all']} events."
    )
    section = _feed_section(
        "Orchestrator",
        items,
        status=status,
        status_c=status_c,
        meta=meta,
        filters=filters,
        empty=empty,
        copy="\n".join(lines),
        footer=_footer(run),
        more=(
            {
                "t": f"Show earlier events (window: {limit})",
                "do": S.set_args(orch_limit=min(limit * 2, 5000)),
            }
            if more
            else None
        ),
    )
    return section


def _footer(run: Any) -> dict[str, Any]:
    """The steering composer, docked under the stream like the node's own.

    The target role is ``any``: the run page has no single node in focus, and
    that is the queue's own default. A finished run gets the note and no input:
    steering rows are consumed by a live node, and an input nothing would read
    is a worse lie than none.
    """
    if not _running(run):
        return {"note": "This run is finished — steering is consumed only by a live run."}
    return {
        "placeholder": "Steer the running node… (Enter to send)",
        "cli": ["board", "steer", run.id, "--role", "any", "--text"],
        "note": "⏵⏵ lands as operator steering · consumed on the node's next turn",
    }


def orchestrator_tab(run: Any, args: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The whole tab: one stream — the supervisor's activity, the steering
    that goes into the run, its own state line, and the composer, together.

    It is deliberately a single section. The tab mirrors the node Stream tab,
    where the transcript, its status pill and the steering input are one
    surface; splitting them into three cards is what made the old design read
    as a dashboard rather than a live transcript.
    """
    return [_feed(run, args)]


def refresh_seconds(run: Any) -> int | None:
    """How often the page should re-fetch itself: a live run only.

    Five seconds is the watcher's own order of magnitude without turning the
    page into a poll loop on a finished run.
    """
    return 5 if _running(run) else None
