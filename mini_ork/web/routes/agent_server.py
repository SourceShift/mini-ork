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
form schemas, and lists agent profiles. Those are implemented here too. It does
NOT yet wire conversations/events to real mini-ork runs — that is a later
slice. Endpoints here are unauthenticated on purpose: the canvas runs in local
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


def _initial_message_text(payload: dict[str, Any]) -> str:
    """Flatten the SDK's SendMessageRequest ({role, content[]}) to plain text.

    Only ``text`` content parts are kept (images/files have no kickoff
    equivalent yet); a plain-string initial_message is tolerated too.
    """
    message = payload.get("initial_message")
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
    when the first chat message arrives (a later slice wires sendMessage).
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
    return _conversation_info(record)


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
