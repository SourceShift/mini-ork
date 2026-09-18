"""OpenHands agent-server protocol shim (SE-3 UI fork, Slice 1).

The forked OpenHands agent-canvas SPA won't leave its "add a backend"
onboarding screen until the host it probes speaks the *agent-server* wire
protocol. The probe (`ui/src/hooks/query/use-backends-health.ts::probeBackend`)
makes exactly three calls against the configured host:

    GET /api/settings   → 200 + a SettingsApiResponse-shaped body
    GET /server_info    → 200 + a ServerInfo with a semver `version`
    assertAgentServerVersionIsSupported(serverInfo)

The version gate (`ui/src/api/agent-server-compatibility.ts`) requires the
reported `version` to parse as exactly major.minor.patch and be >= the
`compatibility.minimumAgentServer` floor pinned in `ui/config/defaults.json`
(currently 1.28.0). We advertise the agent-server version the fork was cut
against so the whole client SDK treats us as a supported peer.

This slice makes the Local backend probe flip green AND lets onboarding walk
its settings write-path to completion. The probe is a read-only handshake, but
onboarding then *writes*: it PATCHes settings, reads the agent/conversation
form schemas, and lists agent profiles. Those are implemented here too.
Conversations ARE wired to real mini-ork runs — create, event history,
sendMessage — but the live WebSocket (`/ws`) is NOT implemented: the canvas
degrades to REST (history via `/events/search`, sends via POST `/events`,
its documented fallback when the socket is not OPEN). Endpoints here are
unauthenticated on purpose: the canvas runs in local
mode (`isAuthRequired()` false) where any session key is accepted, matching the
real agent-server's local posture.

Onboarding write-path (all against the configured host, via the SDK):

    PATCH /api/settings                    → SettingsClient.updateSettings
    GET   /api/settings/agent-schema       → SettingsClient.getAgentSchema
    GET   /api/settings/conversation-schema→ SettingsClient.getConversationSchema
    GET   /api/agent-profiles              → AgentProfilesClient.list

Settings are held in a process-local store (not persisted): mini-ork owns
model/provider selection server-side, so the canvas's settings are cosmetic
app-preferences (analytics consent, language, sound). We accept and echo them
back so the form round-trips, but nothing downstream reads them yet.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ..deps import get_home

# The agent-server wire version this fork was cut against
# (ui/config/defaults.json → versions.agentServer). Must stay a 3-part semver
# at or above compatibility.minimumAgentServer, or the canvas rejects us with
# AgentServerUnknownVersionError / AgentServerUnsupportedVersionError.
AGENT_SERVER_PROTOCOL_VERSION = "1.39.1"

# Process start, for the ServerInfo.uptime field the canvas surfaces in its
# backend-details panel.
_START_MONOTONIC = time.monotonic()

router = APIRouter(tags=["agent-server"])

# Process-local settings store. Seeded to the empty/unconfigured baseline the
# probe already reported. PATCH deep-merges the canvas's diffs into this; GET
# echoes it back. Not persisted — mini-ork owns real config server-side, so
# these are cosmetic app-preferences that only need to survive the session so
# the onboarding form round-trips cleanly.
_SETTINGS: dict[str, Any] = {
    "agent_settings": {},
    "conversation_settings": {},
    "misc_settings": {"app_preferences": {}},
    "llm_api_key_is_set": False,
}

# Minimal, permissive JSON Schema for the agent/conversation settings forms.
# An empty `properties` map means the canvas renders no server-driven fields
# (correct: mini-ork exposes no tunable agent settings to the canvas), and
# `additionalProperties: true` keeps any client-side extras from being rejected.
_EMPTY_SETTINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": True,
}


def _uptime_seconds() -> float:
    return round(time.monotonic() - _START_MONOTONIC, 3)


def _deep_merge(base: dict[str, Any], diff: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `diff` into `base` in place, and return `base`.

    Nested dicts merge key-by-key (so a diff touching one app-preference does
    not clobber the siblings); every other value replaces wholesale. This
    mirrors the agent-server's ``*_diff`` semantics — the canvas sends only the
    fields it changed, not a full settings snapshot.
    """
    for key, value in diff.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _settings_response() -> dict[str, Any]:
    """A SettingsApiResponse-shaped snapshot of the current store."""
    return {
        "agent_settings": _SETTINGS["agent_settings"],
        "conversation_settings": _SETTINGS["conversation_settings"],
        "misc_settings": _SETTINGS["misc_settings"],
        "llm_api_key_is_set": _SETTINGS["llm_api_key_is_set"],
    }


@router.get("/server_info")
def server_info() -> dict[str, Any]:
    """Agent-server identity + version handshake (`ServerClient.getServerInfo`)."""
    uptime = _uptime_seconds()
    return {
        "uptime": uptime,
        # No conversation lifecycle yet, so the server has been "idle" for its
        # whole uptime. Keeps the canvas's idle-timeout UX from misfiring.
        "idle_time": uptime,
        "title": "mini-ork",
        "version": AGENT_SERVER_PROTOCOL_VERSION,
        "sdk_version": AGENT_SERVER_PROTOCOL_VERSION,
        # Empty tool list is intentional: `isAgentServerToolAvailable` treats a
        # non-array as "all tools available", but we advertise an explicit list
        # so the canvas doesn't offer capabilities the Slice-1 shim can't honor.
        "usable_tools": [],
    }


@router.get("/api/settings")
def get_settings() -> dict[str, Any]:
    """Settings snapshot (`SettingsClient.getSettings`).

    The probe only needs a 200 with a SettingsApiResponse-shaped body; the
    canvas reads these fields to decide whether the LLM key is configured. We
    report an empty, unconfigured baseline — mini-ork owns model/provider
    selection server-side, so the canvas never needs to set an LLM key here —
    plus whatever cosmetic diffs a prior PATCH merged in this session.
    """
    return _settings_response()


@router.patch("/api/settings")
def update_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply a settings diff (`SettingsClient.updateSettings`).

    Onboarding sends a partial ``SettingsUpdateRequest`` — only the fields the
    user touched, under ``*_diff`` keys — e.g.::

        {"misc_settings_diff": {"app_preferences": {"user_consents_to_analytics": false}}}

    Without a PATCH handler this exact path answers 405 (GET exists, PATCH does
    not), stalling onboarding. We deep-merge each present diff into the store
    and echo the merged SettingsApiResponse back, matching the SDK's contract
    (``updateSettings`` returns the updated settings body).
    """
    for diff_key, target_key in (
        ("agent_settings_diff", "agent_settings"),
        ("conversation_settings_diff", "conversation_settings"),
        ("misc_settings_diff", "misc_settings"),
    ):
        diff = payload.get(diff_key)
        if isinstance(diff, dict):
            _deep_merge(_SETTINGS[target_key], diff)
    return _settings_response()


@router.get("/api/settings/agent-schema")
def agent_schema() -> dict[str, Any]:
    """Agent-settings form schema (`SettingsClient.getAgentSchema`).

    Returns an empty-but-valid JSON Schema so the canvas renders no
    server-driven agent fields. Implemented explicitly so this path resolves to
    JSON instead of being swallowed by the SPA ``index.html`` catch-all (which
    would hand the canvas HTML with a 200 — a false success it can't parse).
    """
    return _EMPTY_SETTINGS_SCHEMA


@router.get("/api/settings/conversation-schema")
def conversation_schema() -> dict[str, Any]:
    """Conversation-settings form schema (`SettingsClient.getConversationSchema`)."""
    return _EMPTY_SETTINGS_SCHEMA


@router.get("/api/agent-profiles")
def list_agent_profiles() -> list[dict[str, Any]]:
    """Agent-profile list (`AgentProfilesClient.list`).

    We advertise agent-server 1.39.1, so the canvas (profiles shipped in
    1.29.0) will call this during onboarding. The Slice-1 shim has no profile
    lifecycle yet, so we return an empty list: honest, and it avoids promising
    per-profile detail/activate/materialize endpoints we don't serve. Returned
    explicitly so the path is JSON, not the HTML catch-all.
    """
    return []


# ── Conversation lifecycle (Slice 2 keystone) ─────────────────────────────────
#
# The fork's LOCAL backend creates conversations through the SDK's
# ConversationClient — POST /api/conversations with a loose payload and a
# ConversationInfo-shaped response. (POST /api/v1/app-conversations is the
# cloud-only path; the local canvas never calls it.) A conversation IS a
# mini-ork run: the id the client sends (it mints a uuidv4 and passes it as
# ``conversation_id``) becomes the run id, so the canvas's routing and the
# spawned run agree on one key with no translation table — the deterministic
# attach MINI_ORK_RUN_ID injection enables.
#
# Registry: <home>/conversations/<id>.json sidecars hold the ConversationInfo
# projection (title, timestamps, execution_status, recipe). P0 trusts the
# sidecar; refining execution_status from live run state is a later slice.
# Unauthenticated like the rest of the shim (local posture — the canvas sends
# no bearer token), but note this endpoint launches real, billable runs.

#: Recipe a canvas-launched conversation runs. Server-owned: the canvas's
#: agent_settings/llm_model are cosmetic here (mini-ork owns lanes).
CONVERSATION_RECIPE_ENV = "MO_AGENT_SERVER_RECIPE"
DEFAULT_CONVERSATION_RECIPE = "code-fix"


def _conversations_dir(home: Path) -> Path:
    return Path(home) / "conversations"


def _conversation_path(home: Path, conversation_id: str) -> Path:
    return _conversations_dir(home) / f"{conversation_id}.json"


def _safe_conversation_id(conversation_id: str) -> bool:
    return (
        bool(conversation_id)
        and ".." not in conversation_id
        and all(c.isalnum() or c in "-_" for c in conversation_id)
        and len(conversation_id) <= 64
    )


def _load_conversation(home: Path, conversation_id: str) -> dict[str, Any] | None:
    path = _conversation_path(home, conversation_id)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _save_conversation(home: Path, record: dict[str, Any]) -> None:
    path = _conversation_path(home, str(record["id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)


def _flatten_message(message: Any) -> str:
    """Flatten the SDK's message shape ({role, content[]}) to plain text.

    Only ``text`` content parts are kept (images/files have no kickoff
    equivalent yet); a plain-string content is tolerated too.
    """
    if isinstance(message, str):
        return message.strip()
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n\n".join(parts)


def _initial_message_text(payload: dict[str, Any]) -> str:
    """CreateConversation payload → kickoff text (flattens initial_message)."""
    return _flatten_message(payload.get("initial_message"))


def _conversation_info(record: dict[str, Any]) -> dict[str, Any]:
    """Project a registry record to the ConversationInfo contract.

    Required-by-type fields the canvas reads are filled honestly: mini-ork
    owns the agent/LLM server-side, so ``agent.llm.model`` names the lane
    policy rather than a client LLM, and ``persistence_dir`` is the run
    directory namespace the id maps to.
    """
    return {
        "id": record["id"],
        "execution_status": record.get("execution_status", "idle"),
        "confirmation_policy": {"type": "never"},
        "activated_knowledge_skills": [],
        "agent": {"kind": "mini-ork", "llm": {"model": "mini-ork-routed"}},
        "workspace": record.get("workspace"),
        "persistence_dir": str(record.get("persistence_dir", "")),
        "max_iterations": record.get("max_iterations"),
        "title": record.get("title"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "leaf_event_id": None,
    }


@router.post("/api/conversations")
def create_conversation(
    payload: dict[str, Any], home=Depends(get_home)
) -> dict[str, Any]:
    """Create a conversation (`ConversationClient.createConversation`).

    With an ``initial_message`` the mini-ork run launches detached
    immediately (control.launch_run — the same spawn seam POST /api/v1/runs
    uses); without one the conversation is registered idle and the run starts
    when the first chat message arrives (POST .../events below).
    The response is a ConversationInfo; ``execution_status`` is "running"
    once launched, "idle" otherwise.
    """
    from .. import control

    conversation_id = str(payload.get("conversation_id") or "").strip() or str(
        uuid.uuid4()
    )
    if not _safe_conversation_id(conversation_id):
        raise HTTPException(status_code=400, detail="invalid conversation_id")

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    title = str(payload.get("title") or "").strip() or None
    initial_text = _initial_message_text(payload)
    if title is None and initial_text:
        title = initial_text.splitlines()[0][:80]

    recipe = os.environ.get(CONVERSATION_RECIPE_ENV, "").strip() or DEFAULT_CONVERSATION_RECIPE

    record: dict[str, Any] = {
        "id": conversation_id,
        "title": title,
        "created_at": now,
        "updated_at": now,
        "execution_status": "idle",
        "recipe": recipe,
        "run_launched": False,
        # Ledger of user-authored message texts (kickoff + every send). The
        # events projection replays these as user MessageEvents — the source
        # of truth for "what did the human actually say" without re-reading
        # kickoff files or parsing run payloads.
        "messages": [],
    }

    if initial_text:
        kickoff = f"# {title or 'Conversation'}\n\n{initial_text}\n" if title else f"{initial_text}\n"
        result = control.launch_run(home, recipe, kickoff, run_id=conversation_id)
        if not result.get("ok"):
            raise HTTPException(
                status_code=500,
                detail=f"run launch failed: {result.get('error', 'unknown')}",
            )
        record["run_launched"] = True
        record["execution_status"] = "running"
        # launch_run answers {ok, run_id, recipe, pid, kickoff_path, log_path}
        # — no run_dir. The run's artifacts land under <home>/runs/<run_id>/
        # (created by the run lifecycle, not the launcher), so we persist the
        # log path as the one launcher-visible handle on the run's filesystem.
        record["persistence_dir"] = str(result.get("log_path") or "")
        record["messages"].append({"text": initial_text, "ts": now})

    _save_conversation(home, record)
    return _conversation_info(record)


@router.get("/api/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str, home=Depends(get_home)
) -> dict[str, Any]:
    """Hydrate a conversation (`ConversationClient.getConversation`)."""
    if not _safe_conversation_id(conversation_id):
        raise HTTPException(status_code=400, detail="invalid conversation_id")
    record = _load_conversation(home, conversation_id)
    if record is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    _refresh_execution_status(home, record)
    return _conversation_info(record)


# ── Conversation events + sendMessage (Slice 3) ───────────────────────────────
#
# The canvas renders a conversation from two REST surfaces (both via the SDK):
#
#     GET  /api/conversations/{id}/events/search  → ConversationClient.searchEvents
#     GET  /api/conversations/{id}/events/count   → ConversationClient.getEventCount
#     GET  /api/conversations/{id}/events/{eid}   → ConversationClient.getEvent
#     POST /api/conversations/{id}/events         → ConversationClient.sendEvent
#
# History pagination is TIMESTAMP-driven, not cursor-driven: the initial load
# asks `sort_order=TIMESTAMP_DESC&limit=50`, load-older asks the same plus
# `timestamp__lt=<oldest ts>`, and the WebSocket `since` replay uses
# `timestamp__gte`. `page_id` (continue-after-id) is only used by the ascending
# transcript-export path. All four knobs are honored here.
#
# Projection (mini-ork → oh_event): the canvas type-guards events structurally
# (ui/src/types/agent-server/type-guards.ts) — a BaseEvent is {id, timestamp,
# source ∈ user|agent|environment|hook}; a transcript MessageEvent adds
# llm_message{role, content[]}. We project:
#   - sidecar `messages` ledger        → user MessageEvents (what the human said)
#   - run_events node_end rows         → assistant MessageEvents (node summary)
#   - run_events other rows            → environment events (raw lifecycle)
#   - terminal task_runs status        → final assistant MessageEvent
# A conversation with no launched run still serves its user messages, so an
# idle conversation shows its own (empty) transcript instead of erroring.

# task_runs terminal set (mini_ork/cli/execute.py set_status). Everything
# else (running/blocked/needs_revision/…) means the run is still live.
_TERMINAL_RUN_STATUSES = {"published", "rolled_back", "failed"}
_TERMINAL_STATUS_MAP = {"published": "finished", "rolled_back": "error", "failed": "error"}

_MAX_EVENT_PAGE = 100  # EventService caps pages at 100; enforce server-side too


def _state_db(home: Path):
    """Read-only StateDB on <home>/state.db, or None when absent.

    A canvas-only home (conversations created, no runs yet) has no state.db —
    every caller must tolerate None (projects zero run events).
    """
    from ..db import StateDB

    db_path = Path(home) / "state.db"
    if not db_path.is_file():
        return None
    try:
        return StateDB(db_path)
    except FileNotFoundError:
        return None


def _epoch_to_iso(seconds: Any) -> str:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(seconds)))
    except (TypeError, ValueError):
        return "1970-01-01T00:00:00Z"


def _message_event(
    event_id: str, timestamp: str, role: str, text: str
) -> dict[str, Any]:
    """A transcript-renderable MessageEvent (see ui MessageEvent type).

    ``source`` (who emitted it: user|agent) and ``llm_message.role`` (chat
    role: user|assistant) are DISTINCT axes — the canvas type-guards on
    both, and assistant messages must carry source="agent" with
    role="assistant", never role="agent".
    """
    source = "user" if role == "user" else "agent"
    return {
        "id": event_id,
        "timestamp": timestamp,
        "source": source,
        "llm_message": {"role": role, "content": [{"type": "text", "text": text}]},
        "activated_microagents": [],
        "extended_content": [],
    }


def _refresh_execution_status(home: Path, record: dict[str, Any]) -> None:
    """Lift execution_status from the live task_runs row, in place.

    The sidecar records launch-time state ("running"); the run's real
    trajectory lives in task_runs. Terminal statuses map onto the canvas
    ConversationExecutionStatus vocabulary; a live/unknown row keeps
    "running"; no row yet (run still bootstrapping) keeps the sidecar value.
    """
    if not record.get("run_launched"):
        return
    db = _state_db(home)
    if db is None:
        return
    try:
        row = db.row("SELECT status FROM task_runs WHERE id = ?", (record["id"],))
    except Exception:
        return
    if not row:
        return
    status = str(row.get("status") or "")
    if status in _TERMINAL_STATUS_MAP:
        record["execution_status"] = _TERMINAL_STATUS_MAP[status]
    else:
        record["execution_status"] = "running"


def _conversation_events(home: Path, record: dict[str, Any]) -> list[dict[str, Any]]:
    """All oh_events for a conversation, oldest first (deterministic order).

    Sorts by (timestamp, id): timestamps are ISO-Zulu from one formatter, so
    lexical comparison is chronological; the id tiebreak keeps equal-second
    events stable across polls (the canvas dedupes by id).
    """
    cid = str(record["id"])
    events: list[dict[str, Any]] = []

    for idx, msg in enumerate(record.get("messages") or []):
        text = str(msg.get("text") or "").strip()
        if not text:
            continue
        ts = str(msg.get("ts") or record.get("created_at") or "")
        events.append(_message_event(f"{cid}-user-{idx}", ts, "user", text))

    db = _state_db(home)
    if record.get("run_launched") and db is not None and db.has_table("run_events"):
        rows = db.rows(
            """
            SELECT event_id AS id, event_type, payload_json, created_at
            FROM run_events WHERE run_id = ? ORDER BY created_at ASC
            """,
            (cid,),
        )
        for row in rows:
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except ValueError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            ts = _epoch_to_iso(row.get("created_at"))
            node_id = str(payload.get("node_id") or "node")
            node_type = str(payload.get("node_type") or row.get("event_type") or "")
            if row.get("event_type") == "node_end":
                text = f"{node_id} ({node_type}) completed"
                reason = payload.get("finish_reason") or payload.get("status")
                if reason:
                    text += f": {reason}"
                events.append(_message_event(str(row["id"]), ts, "assistant", text))
            else:
                events.append(
                    {
                        "id": str(row["id"]),
                        "timestamp": ts,
                        "source": "environment",
                        "event_type": str(row.get("event_type") or ""),
                        "node_id": node_id,
                        "node_type": node_type,
                    }
                )

        if db.has_table("task_runs"):
            tr = db.row(
                "SELECT status, verdict, ended_at FROM task_runs WHERE id = ?", (cid,)
            )
            if tr and str(tr.get("status") or "") in _TERMINAL_RUN_STATUSES:
                status = str(tr["status"])
                text = f"Run {status}"
                if tr.get("verdict"):
                    text += f" — verdict {tr['verdict']}"
                events.append(
                    _message_event(
                        f"{cid}-final",
                        _epoch_to_iso(tr.get("ended_at")),
                        "assistant",
                        text,
                    )
                )

    events.sort(key=lambda e: (e["timestamp"], e["id"]))
    return events


def _load_conversation_or_404(home: Path, conversation_id: str) -> dict[str, Any]:
    if not _safe_conversation_id(conversation_id):
        raise HTTPException(status_code=400, detail="invalid conversation_id")
    record = _load_conversation(home, conversation_id)
    if record is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return record


@router.get("/api/conversations/{conversation_id}/events/search")
def search_conversation_events(
    conversation_id: str,
    home=Depends(get_home),
    limit: int = 100,
    sort_order: str = "TIMESTAMP_DESC",
    page_id: str | None = None,
    timestamp__gte: str | None = None,
    timestamp__lt: str | None = None,
) -> dict[str, Any]:
    """Event history page (`ConversationClient.searchEvents`).

    Returns ``{items, next_page_id}`` — items in the requested order (the
    canvas reverses DESC pages itself), next_page_id set only when the page
    was actually truncated so `hasMore` heuristics don't loop.
    """
    record = _load_conversation_or_404(home, conversation_id)
    events = _conversation_events(home, record)

    if timestamp__gte:
        events = [e for e in events if e["timestamp"] >= timestamp__gte]
    if timestamp__lt:
        events = [e for e in events if e["timestamp"] < timestamp__lt]
    if page_id:
        # Ascending continue-after cursor (transcript-export path). If the
        # cursor id is unknown (pruned/filtered), treat as page start.
        for idx, e in enumerate(events):
            if e["id"] == page_id:
                events = events[idx + 1 :]
                break

    ascending = sort_order == "TIMESTAMP"
    events.sort(key=lambda e: (e["timestamp"], e["id"]), reverse=not ascending)

    page_limit = max(1, min(limit, _MAX_EVENT_PAGE))
    truncated = len(events) > page_limit
    page = events[:page_limit]
    next_page_id = page[-1]["id"] if truncated and page else None
    return {"items": page, "next_page_id": next_page_id}


@router.get("/api/conversations/{conversation_id}/events/count")
def count_conversation_events(
    conversation_id: str, home=Depends(get_home)
) -> int:
    """Total event count (`ConversationClient.getEventCount`).

    The WebSocket context and transcript export use this to detect events
    they haven't seen; it counts ALL projected events, pre-filter.
    """
    record = _load_conversation_or_404(home, conversation_id)
    return len(_conversation_events(home, record))


@router.get("/api/conversations/{conversation_id}/events/{event_id}")
def get_conversation_event(
    conversation_id: str, event_id: str, home=Depends(get_home)
) -> dict[str, Any]:
    """Single event (`ConversationClient.getEvent`; 404 on unknown id).

    The SDK's getEvents batch swallows 404s, so a pruned id degrades to null
    instead of erroring the batch.
    """
    record = _load_conversation_or_404(home, conversation_id)
    for event in _conversation_events(home, record):
        if event["id"] == event_id:
            return event
    raise HTTPException(status_code=404, detail="event not found")


@router.post("/api/conversations/{conversation_id}/events")
def send_conversation_event(
    payload: dict[str, Any], conversation_id: str, home=Depends(get_home)
) -> dict[str, Any]:
    """Send a message / start the agent loop (`ConversationClient.sendEvent`).

    Body is a SendMessageRequest plus ``run: true``. Three regimes:
      - run not launched → this message BECOMES the kickoff (launch_run,
        same seam as conversation-create): the idle→running promise.
      - run live         → operator steering injection (control.steer_run):
        the message rides the next context_assemble pack of any in-flight
        node; it does NOT spawn a second run.
      - run terminal     → 409: a mini-ork conversation is a one-shot DAG;
        follow-ups need a new conversation (documented divergence from the
        real agent-server, which would loop the agent again).
    Sent text is appended to the sidecar ledger either way, so it projects
    as a user MessageEvent immediately.
    """
    from .. import control

    record = _load_conversation_or_404(home, conversation_id)
    # The send payload IS the message ({role, content[], run}) — unlike
    # create, which nests it under `initial_message`.
    text = _flatten_message(payload)
    if not text:
        raise HTTPException(status_code=400, detail="message text required")

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if not record.get("run_launched"):
        if not record.get("title"):
            record["title"] = text.splitlines()[0][:80]
        kickoff = (
            f"# {record['title']}\n\n{text}\n" if record.get("title") else f"{text}\n"
        )
        recipe = str(record.get("recipe") or DEFAULT_CONVERSATION_RECIPE)
        result = control.launch_run(home, recipe, kickoff, run_id=conversation_id)
        if not result.get("ok"):
            raise HTTPException(
                status_code=500,
                detail=f"run launch failed: {result.get('error', 'unknown')}",
            )
        record["run_launched"] = True
        record["execution_status"] = "running"
        record["persistence_dir"] = str(result.get("log_path") or "")
    else:
        _refresh_execution_status(home, record)
        if record["execution_status"] in ("finished", "error"):
            raise HTTPException(
                status_code=409,
                detail="conversation run already finished — start a new conversation",
            )
        db = _state_db(home)
        if db is None:
            raise HTTPException(
                status_code=500, detail="state db unavailable for steering"
            )
        result = control.steer_run(
            db, conversation_id, text, source="agent-server-canvas"
        )
        if not result.get("ok"):
            raise HTTPException(
                status_code=500,
                detail=f"steering failed: {result.get('error', 'unknown')}",
            )

    record.setdefault("messages", []).append({"text": text, "ts": now})
    record["updated_at"] = now
    _save_conversation(home, record)
    return {"ok": True}


@router.get("/alive")
def alive() -> dict[str, Any]:
    """Liveness (`ServerClient.getAlive`)."""
    return {"status": "ok"}


@router.get("/health")
def health() -> dict[str, Any]:
    """Health (`ServerClient.getHealth`)."""
    return {"status": "ok", "uptime": _uptime_seconds()}


@router.get("/ready")
def ready() -> dict[str, Any]:
    """Readiness (`ServerClient.getReady`; the SDK accepts 200 or 503)."""
    return {"status": "ready"}
