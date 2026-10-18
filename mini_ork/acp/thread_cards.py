"""Pure builders for the inline thread card payload (``_meta["mini_ork"]``).

One JSON object rides the standard ACP ``_meta`` field of an existing
tool-call update; no new protocol message type and no change to the thread
JSONL record shape.  The same builders are called on the live projection and
on ``_replay_run_in_thread`` so a reopened thread reconstructs byte-identical
cards from run data.

This module is deliberately dependency-free and imports nothing from
``mini_ork.acp.agent`` — ``agent.py`` imports it, never the other way round.
Builders never raise on malformed input; a bad payload yields a minimal card.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

CARD_KEY = "mini_ork"
CARD_VERSION = 1

# Caps from the design doc §"Caps and validation".  The builder clamps; the
# renderer validates again and falls back on a malformed payload.
_MAX_TITLE = 200
_MAX_BLOCKS = 12
_MAX_TEXT = 4000
_MAX_CODE = 8000
_MAX_BYTES = 64 * 1024

_HEADER_KEYS = ("mark", "title", "meta", "detail", "tag")
_DEFAULT_HEADER = {"mark": "none", "title": "", "meta": "", "detail": "", "tag": ""}


# ── small helpers ───────────────────────────────────────────────────────────


def _as_str(value: Any) -> str:
    """Coerce to ``str`` without ever raising (``None`` → ``""``)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:  # noqa: BLE001 — a hostile __str__ must not break a card
        return ""


def _payload(event: Any) -> dict[str, Any]:
    """``event["payload_json"]`` as a dict, tolerating a JSON string."""
    if not isinstance(event, dict):
        return {}
    payload = event.get("payload_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            payload = {}
    return payload if isinstance(payload, dict) else {}


def _serialized_size(card: dict[str, Any]) -> int:
    """Byte size of the card as the client will serialize it."""
    try:
        return len(json.dumps(card, ensure_ascii=False, default=_as_str).encode("utf-8"))
    except Exception:  # noqa: BLE001 — circular/non-JSON input never raises
        return 0


def _sanitize_block(block: Any) -> Any:
    """Copy one block and apply the per-block text caps."""
    if not isinstance(block, dict):
        return block
    out = dict(block)
    block_type = out.get("type")
    text = out.get("text")
    if isinstance(text, str):
        limit = _MAX_CODE if block_type == "code" else _MAX_TEXT
        if len(text) > limit:
            out["text"] = text[:limit]
    return out


def _status_mark(status: str) -> str:
    if status in ("completed", "published", "ok", "passed", "approved"):
        return "check"
    if status in ("failed", "refused", "refuted", "error", "cancelled"):
        return "cross"
    if status in ("in_progress", "running", "executing", "pending", "reviewing"):
        return "hand"
    return "dot"


# ── envelope ────────────────────────────────────────────────────────────────


def build_card(
    *,
    kind: str,
    card_id: str,
    revision: int,
    parent: str | None = None,
    phase: str = "execute",
    status: str = "in_progress",
    header: dict | None = None,
    blocks: list[dict] | None = None,
    actions: list[dict] | None = None,
    suppress_stock: bool = True,
    fallback_text: str = "",
) -> dict:
    """Build one card envelope, clamping every field to the design's caps.

    The result is a plain JSON-serializable dict.  Unknown header keys are
    ignored; unknown block/action shapes pass through untouched so a newer
    host vocabulary degrades to "skip this block", never to a lost card.
    """
    raw_header = header if isinstance(header, dict) else {}
    out_header: dict[str, Any] = {}
    for key in _HEADER_KEYS:
        value = raw_header.get(key, _DEFAULT_HEADER[key])
        out_header[key] = "" if value is None else value
    out_header["title"] = _as_str(out_header.get("title"))[:_MAX_TITLE]
    for key in ("mark", "meta", "detail", "tag"):
        out_header[key] = _as_str(out_header.get(key))

    raw_blocks = blocks if isinstance(blocks, list) else []
    out_blocks: list[Any] = [
        _sanitize_block(block) for block in raw_blocks[:_MAX_BLOCKS]
    ]
    raw_actions = actions if isinstance(actions, list) else []
    out_actions: list[Any] = [
        dict(action) if isinstance(action, dict) else action
        for action in raw_actions
    ]

    text = _as_str(fallback_text)
    if not text and out_header["title"]:
        text = out_header["title"]

    card: dict[str, Any] = {
        "v": CARD_VERSION,
        "kind": _as_str(kind),
        "id": _as_str(card_id),
        "revision": int(revision) if isinstance(revision, int) else 0,
        "parent": parent if isinstance(parent, str) and parent else None,
        "phase": _as_str(phase) or "execute",
        "status": _as_str(status) or "in_progress",
        "header": out_header,
        "blocks": out_blocks,
        "actions": out_actions,
        "suppress_stock": bool(suppress_stock),
        "fallback_text": text,
    }

    # Size clamp: drop trailing blocks first, then code blocks (the least
    # essential for the collapsed row), always keeping ``fallback_text``.
    while out_blocks and _serialized_size(card) > _MAX_BYTES:
        out_blocks.pop()
    if _serialized_size(card) > _MAX_BYTES:
        out_blocks = [
            block
            for block in out_blocks
            if not (isinstance(block, dict) and block.get("type") == "code")
        ]
        card["blocks"] = out_blocks
        while out_blocks and _serialized_size(card) > _MAX_BYTES:
            out_blocks.pop()
    return card


def with_meta(update: Any, card: dict) -> Any:
    """Return ``update`` with the card under ``_meta[CARD_KEY]``.

    ACP exposes the wire ``_meta`` field as the ``field_meta`` attribute; the
    input model is never mutated.  Anything without that attribute (a dict, a
    foreign object) is returned unchanged.
    """
    if not hasattr(update, "field_meta") or not hasattr(update, "model_copy"):
        return update
    try:
        return update.model_copy(update={"field_meta": {CARD_KEY: card}})
    except Exception:  # noqa: BLE001 — a card must never break an update
        return update


# ── run / node cards ────────────────────────────────────────────────────────


def _node_rows(events: list[Any]) -> list[dict[str, Any]]:
    """Fold ``node_start``/``node_end`` events into one row per node id."""
    rows: list[dict[str, Any]] = []
    index: dict[str, dict[str, Any]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = event.get("event_type")
        if event_type not in ("node_start", "node_end"):
            continue
        payload = _payload(event)
        node_id = _as_str(
            payload.get("node_id") or event.get("node_id") or "node"
        ) or "node"
        label = _as_str(
            payload.get("node_type") or event.get("node_type") or "node"
        ) or "node"
        row = index.get(node_id)
        if row is None:
            row = {"id": node_id, "label": label, "status": "in_progress"}
            index[node_id] = row
            rows.append(row)
        lane = _as_str(payload.get("model_lane"))
        if lane:
            # Only the lane is available at node-emit time; model id and
            # per-node cost are deliberately omitted (design §2.6).
            row["lane"] = lane
        if event_type == "node_start":
            row["status"] = "in_progress"
        else:
            verdict = _as_str(payload.get("verdict"))
            finish_reason = _as_str(payload.get("finish_reason"))
            row["status"] = verdict or "completed"
            detail = " · ".join(
                part for part in (finish_reason, verdict) if part
            )
            if detail:
                row["detail"] = detail
    return rows


def _run_title(snapshot: dict[str, Any], run_id: str) -> str:
    """Task / first-prompt title when the snapshot carries one."""
    for key in ("task", "title", "first_prompt", "prompt"):
        value = snapshot.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().splitlines()[0][:_MAX_TITLE]
    return f"run {run_id}"


def run_card(
    snapshot: dict,
    *,
    run_id: str,
    recipe: str,
    worktree: dict | None = None,
) -> dict:
    """Build the run marker's card (``<run_id>:parent``)."""
    snap = snapshot if isinstance(snapshot, dict) else {}
    status = _as_str(snap.get("status") or "in_progress") or "in_progress"
    events = snap.get("events") if isinstance(snap.get("events"), list) else []
    rows = _node_rows(events)

    blocks: list[dict[str, Any]] = []
    summary = ""
    for key in ("summary", "agent_summary", "summary_text"):
        value = snap.get(key)
        if isinstance(value, str) and value.strip():
            summary = value.strip()
            break
    if summary:
        blocks.append({"type": "text", "text": summary})
    if rows:
        blocks.append({"type": "steps", "steps": rows})

    kv_rows: list[dict[str, str]] = []
    if isinstance(worktree, dict):
        mode = _as_str(worktree.get("mode"))
        branch = _as_str(worktree.get("branch"))
        if mode:
            kv_rows.append({"k": "workspace", "v": mode})
        if branch:
            kv_rows.append({"k": "branch", "v": branch})
    calls = snap.get("llm_calls") if isinstance(snap.get("llm_calls"), list) else []
    cost = 0.0
    for call in calls:
        if isinstance(call, dict):
            try:
                cost += float(call.get("cost_usd") or 0.0)
            except (TypeError, ValueError):
                continue
    if cost:
        kv_rows.append({"k": "cost", "v": f"${cost:.2f}"})
    duration = snap.get("duration_ms")
    if duration is not None:
        kv_rows.append({"k": "duration", "v": f"{_as_str(duration)} ms"})
    if kv_rows:
        blocks.append({"type": "kv", "rows": kv_rows})

    title = _run_title(snap, run_id)
    return build_card(
        kind="run",
        card_id=f"{run_id}:parent",
        revision=0,
        parent=None,
        phase="execute",
        status=status,
        header={
            "mark": _status_mark(status),
            "title": title,
            "detail": status,
            "tag": _as_str(recipe),
        },
        blocks=blocks,
        fallback_text=f"run {run_id} ({recipe}) · {status}",
    )


def node_card(
    event: dict,
    *,
    run_id: str,
    recipe: str,
    revision: int = 0,
) -> dict:
    """Build a node's card from one lifecycle event."""
    ev = event if isinstance(event, dict) else {}
    payload = _payload(ev)
    node_id = _as_str(
        payload.get("node_id") or ev.get("node_id") or "node"
    ) or "node"
    node_type = _as_str(
        payload.get("node_type") or ev.get("node_type") or "node"
    ) or "node"
    event_type = _as_str(ev.get("event_type"))
    lane = _as_str(payload.get("model_lane"))
    verdict = _as_str(payload.get("verdict"))
    finish_reason = _as_str(payload.get("finish_reason"))

    if event_type == "node_end":
        status = verdict or "completed"
    else:
        status = "in_progress"

    row: dict[str, Any] = {"id": node_id, "label": node_type, "status": status}
    if lane:
        row["lane"] = lane
    detail = " · ".join(part for part in (finish_reason, verdict) if part)
    if detail:
        row["detail"] = detail

    return build_card(
        kind="node",
        card_id=node_id,
        revision=revision,
        parent=f"{run_id}:parent",
        phase="execute",
        status=status,
        header={
            "mark": _status_mark(status),
            "title": f"{node_type} {node_id}",
            "detail": status,
            "tag": _as_str(recipe),
        },
        blocks=[{"type": "steps", "steps": [row]}],
        suppress_stock=True,
        fallback_text=f"{node_type} {node_id}: {status}",
    )


# ── kickoff card ────────────────────────────────────────────────────────────


def _markdown_sections(markdown: str) -> list[dict[str, Any]]:
    """Split ``markdown`` into ``{title, level, body}`` heading sections."""
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in markdown.splitlines():
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if match:
            current = {
                "title": match.group(2).strip(),
                "level": len(match.group(1)),
                "lines": [],
            }
            sections.append(current)
        elif current is not None:
            current["lines"].append(line)
    for section in sections:
        section["body"] = "\n".join(section.pop("lines")).strip()
    return sections


def _in_scope_paths(body: str) -> list[str]:
    """Backticked paths (and fenced path lines) under an IN SCOPE heading."""
    paths: list[str] = []

    def add(value: str) -> None:
        value = value.strip().strip("\"'")
        if value and value not in paths:
            paths.append(value)

    for token in re.findall(r"`([^`\n]+)`", body):
        add(token)
    in_fence = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence and stripped:
            add(stripped.lstrip("-* ").strip())
    return paths


def _checklist_items(body: str) -> list[dict[str, Any]]:
    """Checklist rows (``- [ ]`` / ``- [x]`` / ``-``) under PROVES SUCCESS."""
    items: list[dict[str, Any]] = []
    for line in body.splitlines():
        match = re.match(r"^\s*[-*]\s+(?:\[([ xX])\]\s*)?(.+?)\s*$", line)
        if not match:
            continue
        text = match.group(2).strip()
        if not text:
            continue
        checked = _as_str(match.group(1)).lower() == "x"
        items.append(
            {
                "mark": "check" if checked else "dot",
                "text": text,
                "status": "ok" if checked else "pending",
            }
        )
    return items


def kickoff_card(draft_path: str, markdown: str, *, recipe: str) -> dict:
    """Build the kickoff draft's card from its staged markdown.

    Missing sections are omitted, never fatal; an empty document still yields
    a valid pending card.
    """
    text = markdown if isinstance(markdown, str) else ""
    sections = _markdown_sections(text)
    stem = Path(_as_str(draft_path)).stem or "kickoff"

    blocks: list[dict[str, Any]] = []
    title = ""
    for section in sections:
        heading = section["title"]
        upper = heading.upper()
        body = section["body"]
        if upper == "TASK":
            if body:
                blocks.append({"type": "section", "title": "TASK", "text": body})
                first_line = body.splitlines()[0].strip()
                if first_line and not title:
                    title = first_line[:_MAX_TITLE]
        elif "IN SCOPE" in upper:
            paths = _in_scope_paths(body)
            if paths:
                entries = []
                for path in paths:
                    try:
                        exists = Path(path).exists()
                    except (OSError, ValueError):
                        exists = True
                    entries.append(
                        {
                            "path": path,
                            "label": "" if exists else "new",
                            "status": "muted" if exists else "new",
                        }
                    )
                blocks.append({"type": "files", "files": entries})
        elif "PROVES SUCCESS" in upper:
            if body:
                blocks.append(
                    {"type": "section", "title": "PROVES SUCCESS", "text": body}
                )
            items = _checklist_items(body)
            if items:
                blocks.append({"type": "items", "items": items})
        elif "NOTE" in upper and body:
            blocks.append({"type": "section", "title": heading, "text": body})

    if not title:
        title = sections[0]["title"] if sections else f"kickoff {stem}"

    actions = [
        {"id": "run", "label": "Start run", "kind": "permission", "option_id": "run"},
        {"id": "save", "label": "Save only", "kind": "permission", "option_id": "save"},
        {
            "id": "change",
            "label": "Change something",
            "kind": "permission",
            "option_id": "change",
        },
        {
            "id": "discard",
            "label": "Discard",
            "kind": "permission",
            "option_id": "discard",
        },
    ]

    return build_card(
        kind="kickoff",
        card_id=stem,
        revision=0,
        parent=None,
        phase="plan",
        status="pending",
        header={
            "mark": "hand",
            "title": title,
            "detail": "draft",
            "tag": _as_str(recipe),
        },
        blocks=blocks,
        actions=actions,
        fallback_text=f"kickoff {stem} ({recipe}) · pending",
    )


# ── failure / review cards ──────────────────────────────────────────────────


def _failed_node(hint: dict[str, Any], snapshot: dict[str, Any]) -> str:
    """Best available failed-node id: hint first, then the snapshot events."""
    node = _as_str(hint.get("failed_node") or hint.get("from_node"))
    if node:
        return node
    events = snapshot.get("events") if isinstance(snapshot.get("events"), list) else []
    fallback = ""
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("event_type") not in ("node_start", "node_end"):
            continue
        payload = _payload(event)
        node_id = _as_str(payload.get("node_id") or event.get("node_id"))
        if not node_id:
            continue
        fallback = node_id
        if event.get("event_type") == "node_end":
            if _as_str(payload.get("verdict") or payload.get("finish_reason")):
                return node_id
    return fallback or "implementer"


def failure_card(
    run_id: str,
    snapshot: dict,
    hint: dict | None,
    *,
    diagnosis_path: str | None,
) -> dict:
    """Build a run-failure card; a missing/malformed hint yields a minimal one."""
    snap = snapshot if isinstance(snapshot, dict) else {}
    data = hint if isinstance(hint, dict) else {}
    needs_change = data.get("needs_change")
    if not isinstance(needs_change, dict):
        needs_change = {}
    node = _failed_node(data, snap)

    blocks: list[dict[str, Any]] = []
    summary = _as_str(needs_change.get("summary")).strip()
    gate = _as_str(data.get("gate") or needs_change.get("kind")).strip()
    text = f"Run {run_id} failed"
    if node:
        text += f" at {node}"
    if gate:
        text += f" ({gate} gate)"
    text += f": {summary}" if summary else "."
    blocks.append({"type": "text", "text": text})

    kv_rows: list[dict[str, str]] = []
    if node:
        kv_rows.append({"k": "node", "v": node})
    for key, label in (
        ("attempt", "attempt"),
        ("checkpoint", "checkpoint"),
        ("healer", "healer"),
        ("learning", "related learning"),
    ):
        value = data.get(key)
        if value not in (None, "", []):
            kv_rows.append({"k": label, "v": _as_str(value)})
    if kv_rows:
        blocks.append({"type": "kv", "rows": kv_rows})

    stderr = _as_str(data.get("stderr") or needs_change.get("detail")).strip()
    if stderr:
        blocks.append({"type": "code", "lang": "text", "text": stderr[:_MAX_CODE]})

    actions: list[dict[str, Any]] = [
        {"id": "recover", "label": "Recover", "kind": "permission", "option_id": "recover"},
        {
            "id": f"resume:{node}",
            "label": f"Resume from {node}",
            "kind": "permission",
            "option_id": f"resume:{node}",
        },
        {
            "id": "forensics",
            "label": "Open forensics",
            "kind": "permission",
            "option_id": "forensics",
        },
        {
            "id": "leave",
            "label": "Leave it",
            "kind": "permission",
            "option_id": "leave",
        },
    ]
    if diagnosis_path:
        try:
            exists = Path(diagnosis_path).is_file()
        except (OSError, ValueError):
            exists = False
        if exists:
            actions.append(
                {
                    "id": "forensics-link",
                    "label": "Open diagnosis.json",
                    "kind": "link",
                    "target": _as_str(diagnosis_path),
                }
            )

    return build_card(
        kind="failure",
        card_id=f"{run_id}:failure",
        revision=0,
        parent=f"{run_id}:parent",
        phase="recovery",
        status="failed",
        header={
            "mark": "cross",
            "title": f"Run {run_id} failed",
            "detail": node,
            "tag": gate,
        },
        blocks=blocks,
        actions=actions,
        fallback_text=f"run {run_id} failed at {node}",
    )


def review_card(run_id: str, snapshot: dict, options: list[dict]) -> dict:
    """Build a review card whose permission actions mirror ``options``.

    ``options`` may be dicts (``option_id``/``label``) or ACP
    ``PermissionOption`` objects (``option_id``/``name``).
    """
    snap = snapshot if isinstance(snapshot, dict) else {}
    status = _as_str(snap.get("status") or "pending") or "pending"
    items: list[dict[str, Any]] = [
        {
            "mark": "hand",
            "text": f"Run {run_id} is ready for review ({status}).",
            "status": "pending",
        }
    ]
    detail = _as_str(snap.get("detail")).strip()
    if detail:
        items.append({"mark": "dot", "text": detail, "status": "muted"})

    actions: list[dict[str, Any]] = []
    for option in options or []:
        if isinstance(option, dict):
            option_id = _as_str(option.get("option_id") or option.get("id"))
            label = _as_str(option.get("label") or option.get("name") or option_id)
        else:
            option_id = _as_str(getattr(option, "option_id", ""))
            label = _as_str(getattr(option, "name", "") or option_id)
        if not option_id:
            continue
        actions.append(
            {
                "id": option_id,
                "label": label,
                "kind": "permission",
                "option_id": option_id,
            }
        )

    return build_card(
        kind="review",
        card_id=f"{run_id}:parent",
        revision=0,
        parent=f"{run_id}:parent",
        phase="verify",
        status="pending",
        header={
            "mark": "hand",
            "title": f"Review run {run_id}",
            "detail": status,
        },
        blocks=[{"type": "items", "items": items}],
        actions=actions,
        fallback_text=f"review run {run_id}",
    )
