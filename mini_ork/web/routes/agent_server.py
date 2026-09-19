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
its documented fallback when the socket is not OPEN).

These routes are deliberately TOKENLESS: the canvas runs in local mode
(`isAuthRequired()` false) where any session key is accepted, matching the real
agent-server's local posture — and the bearer-token substrate
(`web/auth.py::require_token`) is fail-closed, so applying it here would 401
every canvas call on a home with no `auth-tokens.txt`. The boundary is drawn by
:func:`require_local_caller` instead, which is what actually distinguishes the
local case: the routes are reachable from this machine only, on the loopback
bind, and a *browser page from anywhere else* is refused before the
side-effecting handler runs. See that function for why Origin beats a token
against the threat that exists here.

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
import re
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ..deps import get_home, get_home_lenient

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


#: Opt-out for a deliberately-exposed deployment (`mini-ork serve --host 0.0.0.0`).
#: Set to 1/true/yes to drop the cross-origin refusal below.
ALLOW_REMOTE_ENV = "MO_AGENT_SERVER_ALLOW_REMOTE"

#: An Origin a browser served FROM THIS MACHINE would send. Deliberately does not
#: include the literal "null": CORS tolerates it for Electron `file://` renderers,
#: but any sandboxed iframe or `data:` URL also manufactures it, so it cannot be
#: evidence of trustworthiness on a route that launches a billable run.
_LOCAL_ORIGIN_RE = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")


def require_local_caller(request: Request) -> None:
    """Refuse a browser request whose page was not served from this machine.

    The shim is tokenless by design (see the module note), so the live threat on
    these routes is not a remote client — it is the browser the operator already
    has open. A page on any site can POST here, and a cross-origin ``text/plain``
    POST is a CORS *simple request*: the preflight is skipped, so the side effect
    LANDS even though the attacker cannot read the response. Since
    ``POST /api/conversations`` launches a real, billable mini-ork run, a
    CSRF-shaped lever on it is worth closing.

    Origin is the correct control here rather than a token. Browsers send it on
    every cross-origin POST (including ``no-cors``), so a foreign page cannot
    suppress it, while a non-browser caller — the SDK, curl, a test's TestClient —
    sends none and is admitted unhindered. A token would buy nothing against this
    threat class anyway: the attacker is the operator's own browser, and any
    process on this machine could read the token file.

    Explicitly NOT covered: a hostile *local* process. Defending that needs OS
    isolation, not an HTTP header; the loopback default bind (``cli/serve.py``)
    is what keeps that population small.
    """
    if os.environ.get(ALLOW_REMOTE_ENV, "").strip().lower() in ("1", "true", "yes"):
        return
    origin = request.headers.get("origin")
    if origin and not _LOCAL_ORIGIN_RE.match(origin):
        raise HTTPException(status_code=403, detail="cross-origin request refused")


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


@router.get("/api/profiles")
def list_profiles() -> dict[str, Any]:
    """mini-ork's dispatch lanes, projected as LLM profiles.

    Distinct from ``/api/agent-profiles`` above (a different client, and the one
    the canvas polls during onboarding): this is what ``useLlmConfigured``
    reads, and in local mode that hook gates the composer on
    ``profiles.find(p => p.name === active_profile)?.api_key_set``
    (``ui/src/hooks/use-llm-configured.ts``). An empty list is therefore not a
    neutral placeholder — it reads as "no LLM configured" and *disables the chat
    input*, which makes the whole canvas inert.

    So the list is the real thing: one row per lane in mini-ork's provider
    registry (``providers.yaml``, the authoritative lane config), with
    ``api_key_set`` from :func:`lane_health` — the framework's own pre-dispatch
    check, which is precisely a credential-*presence* test ("$X is not set —
    lane would die silently"). That matches what ``api_key_set`` means on the
    wire (a key is configured), not a claim that the key still authenticates.

    ``active_profile`` names the lane the operator's agent policy routes to
    most (``agents.yaml`` ``lanes:``), falling back to the first healthy
    registry lane when there is no policy. It is display-only — mini-ork picks
    the lane per recipe *node*, so there is no single active model to switch —
    but registry order is alphabetical, and naming a lane the policy never
    dispatches (an ambient lane the operator has taken out of scope) would
    misdescribe the deployment. A lane with no credential is still listed, with
    ``api_key_set: false``, because hiding it would be the more misleading
    option.

    The shape is ``ProfileListResponse``, NOT a bare array. Returning ``[]``
    reads as *more* complete than the 404 it replaces while being worse: the
    caller does ``response.profiles.find(...)``, and on an array ``.profiles``
    is undefined, so the page dies with "Cannot read properties of undefined
    (reading 'find')" — an empty list that crashes the app. A 404 degrades; a
    wrong 200 detonates. Get the envelope right or stay silent.
    """
    from mini_ork.dispatch.providers import (  # local: keeps app import cheap
        _load_providers_registry,
        lane_health,
    )

    try:
        registry = _load_providers_registry()
    except (ValueError, OSError):
        # A malformed registry is mini-ork's problem, not the canvas's: report
        # an empty profile list rather than 500 the whole sidebar.
        return {"profiles": [], "active_profile": None}

    profiles: list[dict[str, Any]] = []
    healthy_lanes: list[str] = []
    for name, entry in registry.items():
        if not isinstance(entry, dict):
            continue
        healthy = lane_health(name).ok
        model = entry.get("model")
        base_url = entry.get("base_url")
        profiles.append(
            {
                "name": name,
                "model": model if isinstance(model, str) else None,
                "base_url": base_url if isinstance(base_url, str) else None,
                "api_key_set": healthy,
            }
        )
        if healthy:
            healthy_lanes.append(name)

    return {
        "profiles": profiles,
        "active_profile": _policy_lane(healthy_lanes) or (healthy_lanes or [None])[0],
    }


def _policy_lane(healthy_lanes: list[str]) -> str | None:
    """The healthy lane ``agents.yaml`` routes to most, or None.

    "Most" rather than "first": roles share lanes (four code roles -> minimax),
    so the mode of the policy is the better proxy for what a run will actually
    dispatch. Best-effort by design — a missing or unparseable policy is a
    normal state (there is a committed default), not an error worth failing the
    profile list over.
    """
    if not healthy_lanes:
        return None
    try:
        from mini_ork.steering.decision_service import _load_lanes, resolve_agents_yaml

        lanes = _load_lanes(resolve_agents_yaml())
    except Exception:  # noqa: BLE001 — policy is advisory here
        return None
    counts: dict[str, int] = {}
    for lane in lanes.values():
        if lane in healthy_lanes:
            counts[lane] = counts.get(lane, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda lane: counts[lane])


@router.get("/api/workspaces")
def list_workspaces() -> dict[str, Any]:
    """Workspace list. mini-ork runs against ``MO_TARGET_CWD``; there is no
    per-conversation workspace registry to enumerate, so the canvas gets an
    empty list rather than a 404 it re-polls on every navigation.

    ``WorkspacesListResponse`` is an object with both keys — see the shape
    note on ``list_profiles`` for why a bare ``[]`` is not a safe shorthand.
    """
    return {"workspaces": [], "workspaceParents": []}


@router.post("/api/automation/v1/telemetry/consent")
def set_telemetry_consent(payload: dict[str, Any]) -> dict[str, Any]:
    """Record whether the user consented to anonymous usage data.

    The onboarding modal POSTs ``{consent_granted, frontend_distinct_id}`` here
    and, without a handler, got a 405 from the SPA catch-all — so the preference
    was never accepted and the modal re-appeared on every page load. We echo the
    flag straight back: mini-ork sends no telemetry, so the only thing worth
    doing here is accepting the choice and letting the canvas stop asking.
    """
    return {"consent_granted": bool(payload.get("consent_granted"))}


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


def _conversation_records(home: Path) -> list[dict[str, Any]]:
    """Every registry record under ``<home>/conversations``.

    Unreadable or malformed sidecars are SKIPPED rather than raising: this backs
    the canvas's sidebar, and one truncated file (a kill mid-write) must not take
    the whole list down with it. A record without an ``id`` is unusable as a
    ConversationInfo, so it does not count either.
    """
    directory = _conversations_dir(home)
    if not directory.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("id"):
            records.append(data)
    return records


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


@router.post("/api/conversations", dependencies=[Depends(require_local_caller)])
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


# ── Conversation list / batch-get (Slice 4) ───────────────────────────────────
#
# The canvas's sidebar hydrates from three calls (SDK ConversationClient):
#
#     GET /api/conversations/search?limit&page_id&sort_order  → {items, next_page_id}
#     GET /api/conversations/count                            → int
#     GET /api/conversations?ids=a&ids=b                      → [ConversationInfo|null]
#
# REGISTRATION ORDER IS LOAD-BEARING: `/search` and `/count` are literal
# segments that also match the `/api/conversations/{conversation_id}` pattern
# below, and FastAPI resolves in registration order — so both are declared
# BEFORE it. Registered after, `/search` would be read as conversation id
# "search", miss the registry, and 404 the sidebar with a misleading
# "conversation not found". `test_agent_server_conversation_list_routes_precede_id_route`
# pins the order.
#
# Only UPDATED_AT_DESC / CREATED_AT_DESC exist in the SDK's ConversationSortOrder
# enum, but the sort key is derived generically (field from the name, direction
# from the suffix) so a client asking for ASC gets ASC rather than a silent
# reversal.

_MAX_CONVERSATION_PAGE = 100


def _sorted_conversation_records(
    home: Path, sort_order: str
) -> list[dict[str, Any]]:
    """Registry records, refreshed against live run state, in `sort_order`."""
    records = _conversation_records(home)
    for record in records:
        _refresh_execution_status(home, record)
    field = "created_at" if "CREATED_AT" in sort_order else "updated_at"
    # id is the tiebreak so equal-second timestamps keep a stable order across
    # polls — the canvas paginates by id and would otherwise re-serve or skip.
    records.sort(
        key=lambda r: (str(r.get(field) or ""), str(r["id"])),
        reverse=not sort_order.endswith("_ASC"),
    )
    return records


@router.get("/api/conversations/search")
def search_conversations(
    home=Depends(get_home),
    limit: int = 20,
    page_id: str | None = None,
    sort_order: str = "UPDATED_AT_DESC",
) -> dict[str, Any]:
    """Conversation page (`ConversationClient.searchConversations`)."""
    records = _sorted_conversation_records(home, sort_order)

    if page_id:
        # Continue-after-id cursor. An unknown cursor (pruned/filtered) restarts
        # the page rather than erroring — a sidebar that dead-ends is worse than
        # one that repeats a row the client already dedupes by id.
        for idx, record in enumerate(records):
            if str(record["id"]) == page_id:
                records = records[idx + 1 :]
                break

    page_limit = max(1, min(limit, _MAX_CONVERSATION_PAGE))
    truncated = len(records) > page_limit
    page = records[:page_limit]
    return {
        "items": [_conversation_info(record) for record in page],
        "next_page_id": str(page[-1]["id"]) if truncated and page else None,
    }


@router.get("/api/conversations/count")
def count_conversations(home=Depends(get_home)) -> int:
    """Conversation total (`ConversationClient.countConversations`)."""
    return len(_conversation_records(home))


@router.get("/api/conversations")
def get_conversations(
    home=Depends(get_home), ids: list[str] | None = Query(default=None)
) -> list[dict[str, Any] | None]:
    """Batch get by id (`ConversationClient.getConversations`).

    Positional, and ``null`` for any id that is unknown or unsafe — the SDK's
    ``requireDirectConversationItems`` needs an array aligned with the request,
    and the batch path is explicitly tolerant of misses (it is how the canvas
    re-hydrates rows whose sidecar was pruned). Raising per-id would fail the
    whole batch over one stale row.
    """
    if not ids:
        return []
    return [
        _conversation_info(record) if record is not None else None
        for record in (
            _load_conversation(home, cid) if _safe_conversation_id(cid) else None
            for cid in ids
        )
    ]


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


@router.post(
    "/api/conversations/{conversation_id}/events",
    dependencies=[Depends(require_local_caller)],
)
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


# ── Liveness / health / readiness (Slice 4) ───────────────────────────────────
#
# Three distinct questions, previously all answered "ok" unconditionally — which
# made the canvas unable to tell "mini-ork is down" from "mini-ork is up but has
# no workspace", the two failures that need different operator responses:
#
#   /alive   process is running            → no dependency, never 503
#   /health  process + state, as DATA      → 200 always, `status` degrades
#   /ready   can this serve a run?         → 503 when there is no state.db
#
# All three resolve the home via `get_home_lenient`. A health probe that 404s on
# a bad/missing workspace cannot REPORT the problem it exists to report — and
# `get_home` raises exactly that 404. The body names the home actually probed,
# so a caller pointed at a dead workspace sees the fallback rather than a
# misleading "healthy".


def _db_present(home: Path) -> bool:
    return (Path(home) / "state.db").is_file()


@router.get("/alive")
def alive() -> dict[str, Any]:
    """Liveness (`ServerClient.getAlive`). Dependency-free on purpose: this is
    the "is the process up at all" probe, so it must not fail for a reason a
    different endpoint already reports."""
    return {"status": "ok"}


@router.get("/health")
def health(home=Depends(get_home_lenient)) -> dict[str, Any]:
    """Health (`ServerClient.getHealth`). Always 200 — the degradation is in the
    body, so a monitor can scrape it without treating it as transport failure."""
    db_ok = _db_present(home)
    return {
        "status": "ok" if db_ok else "degraded",
        "uptime": _uptime_seconds(),
        "home": str(home),
        "db_present": db_ok,
    }


@router.get("/ready")
def ready(home=Depends(get_home_lenient)) -> JSONResponse:
    """Readiness (`ServerClient.getReady`; the SDK accepts 200 or 503).

    503 without a ``state.db``: launching a run and reading its events both go
    through it, so a home without one cannot serve the canvas. Answering 200 here
    would be the silent-empty failure mode this shim refuses elsewhere.
    """
    if _db_present(home):
        return JSONResponse({"status": "ready", "home": str(home)}, status_code=200)
    return JSONResponse(
        {
            "status": "not_ready",
            "reason": f"no state.db under {home}",
            "home": str(home),
        },
        status_code=503,
    )
