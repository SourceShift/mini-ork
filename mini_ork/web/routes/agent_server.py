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

This slice makes the Local backend probe flip green and lets onboarding
complete. It does NOT yet wire conversations/events to real mini-ork runs —
that is a later slice. Endpoints here are unauthenticated on purpose: the
canvas runs in local mode (`isAuthRequired()` false) where any session key is
accepted, matching the real agent-server's local posture.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter

# The agent-server wire version this fork was cut against
# (ui/config/defaults.json → versions.agentServer). Must stay a 3-part semver
# at or above compatibility.minimumAgentServer, or the canvas rejects us with
# AgentServerUnknownVersionError / AgentServerUnsupportedVersionError.
AGENT_SERVER_PROTOCOL_VERSION = "1.39.1"

# Process start, for the ServerInfo.uptime field the canvas surfaces in its
# backend-details panel.
_START_MONOTONIC = time.monotonic()

router = APIRouter(tags=["agent-server"])


def _uptime_seconds() -> float:
    return round(time.monotonic() - _START_MONOTONIC, 3)


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
    selection server-side, so the canvas never needs to set an LLM key here.
    """
    return {
        "agent_settings": {},
        "conversation_settings": {},
        "llm_api_key_is_set": False,
    }


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
