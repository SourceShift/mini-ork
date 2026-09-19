"""The canvas event WebSocket — ``/sockets/events/{conversation_id}``.

The forked canvas opens one socket per conversation and degrades to REST
polling when it cannot open one (``conversation-websocket-context.tsx`` sends
``resend_mode``/``after_timestamp`` query params and treats a socket that never
reaches OPEN as a permanent "Disconnected" chip). Slice-1 shipped the REST
degradation and left this route unserved; everything below closes that gap.

Three properties this route needs that ``routes/stream.py`` does not have:

* **History replay.** The client loads history over REST first, then subscribes
  with ``resend_mode=since&after_timestamp=<iso>`` so the server resends only
  what it does not already have — or ``resend_mode=all`` for a fresh
  conversation. ``stream.py`` seeds its cursor at ``MAX(id)`` and is
  *new-only*, which is precisely the reconnect case the canvas hits: it would
  silently drop everything written while the socket was down.
* **A terminal frame.** On a finished run the client must learn the run ended.
  That is a ``ConversationStateUpdateEvent`` with ``key="execution_status"``.
  The socket then stays open: see the comment in the poll loop for why closing
  it would break every already-finished conversation.
* **Inbound sends.** The canvas is NOT a passive subscriber. When the socket is
  OPEN its ``sendMessage`` takes the WebSocket branch — ``currentSocket.send({
  ...message, run: true })`` — and returns ``{queued: false}`` without ever
  calling the REST ``POST /events`` path. A socket that only pushes would
  therefore *lose every message the user types*, and losing it silently is
  strictly worse than the "Disconnected" fallback it replaced. So the receive
  loop hands each message to ``agent_server.send_conversation_event`` — the
  very function the REST route is — which is what keeps the two transports
  from drifting: one projection, one delivery path, two encodings.

The projection is not reimplemented here: ``agent_server._conversation_events``
already turns a conversation record plus its ``run_events`` rows into the
canvas event vocabulary, and the REST ``/events/search`` route serves the exact
same list. Serving one projection over two transports is what keeps the socket
and the REST fallback from drifting apart.

``/sockets/bash-events`` is accepted and held open. The canvas's bash runner
pairs it with ``POST /api/bash/execute_bash_command``, which this shim does not
serve yet, so the socket is honest-but-inert: it clears the terminal's
connection chip and emits nothing, rather than reporting a connection error the
user cannot act on.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from ..deps import get_home_lenient
from .agent_server import (
    _TERMINAL_STATUS_MAP,
    _conversation_events,
    _load_conversation,
    _refresh_execution_status,
    _safe_conversation_id,
    send_conversation_event,
)

router = APIRouter(tags=["sockets"])

# Poll cadence for new events. The projection is a pure in-process read over
# sqlite plus the conversation sidecar — no network, no model call — so this is
# the cost of a small query, not of an LLM turn. 0.5s matches the responsive
# end of the 0.5-1s the plan allowed and is well under a human's sense of
# "live" while keeping a leaked connection cheap.
_POLL_SECONDS = 0.5

# Close codes. 1000 is a normal close and stops the client's reconnect loop;
# 1011 tells it the close was our fault, so it retries.
_CLOSE_NORMAL = 1000
_CLOSE_FAILED = 1011

# A connection is bounded so a client that vanishes without a close frame
# cannot pin a polling task forever. Generous: a goal-loop run can be long.
_MAX_LIFETIME_SECONDS = 8 * 60 * 60


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _state_event(event_id: str, key: str, value: Any) -> dict[str, Any]:
    """A ``ConversationStateUpdateEvent`` frame.

    ``source`` is pinned to ``"environment"``: the canvas's type guard accepts
    any of the four sources for a *base* event, but the state-update variant
    narrows to the environment, and a frame carrying ``source="agent"`` would
    be dropped by ``isAgentServerEvent`` before the key is ever inspected.
    """
    return {
        "id": event_id,
        "timestamp": _now(),
        "source": "environment",
        "kind": "ConversationStateUpdateEvent",
        "key": key,
        "value": value,
    }


def _error_event(event_id: str, code: str, detail: str) -> dict[str, Any]:
    """A ``ConversationErrorEvent`` — the frame the canvas renders as a banner.

    Without this an inbound send that fails (a run launch refused, a 409 on a
    finished conversation) would be dropped on the floor: ``sendMessage`` is
    fire-and-forget over the socket, so the banner is the only channel left to
    tell the user their message did not take.
    """
    return {
        "id": event_id,
        "timestamp": _now(),
        "source": "environment",
        "kind": "ConversationErrorEvent",
        "code": code,
        "detail": detail,
    }


def _project(home, record: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """(events oldest-first, execution_status) for a conversation, now.

    Blocking (sqlite); every caller runs it on a worker thread so the event
    loop keeps serving other sockets — the pattern ``routes/stream.py``
    mandates for exactly this reason.
    """
    _refresh_execution_status(home, record)
    status = str(record.get("execution_status") or "idle")
    return _conversation_events(home, record), status


def _reload(home, conversation_id: str, fallback: dict[str, Any]) -> dict[str, Any]:
    """Re-read the conversation sidecar, falling back to the last good copy.

    The sidecar is live state, not a snapshot: ``POST /events`` — and this
    route's own receive loop — append to ``record["messages"]`` and a run's
    launcher rewrites ``execution_status``, both while this socket is open.
    Polling the record object captured at connect would freeze the message log
    at handshake time — the socket would relay run_events (read fresh from
    sqlite each tick) while silently dropping every user message sent during
    the session. A sidecar that disappears mid-stream keeps the previous copy
    rather than blanking the conversation.
    """
    return _load_conversation(home, conversation_id) or fallback


def _is_terminal(status: str) -> bool:
    """True when the run has stopped moving.

    ``_refresh_execution_status`` has already folded ``task_runs.status`` into
    the canvas vocabulary, so a terminal run reads back as one of the mapped
    values rather than a raw recipe status.
    """
    return status in set(_TERMINAL_STATUS_MAP.values())


async def _send(ws: WebSocket, frame: dict[str, Any]) -> bool:
    """Send one frame; False once the peer is gone.

    A closed socket raises from ``send_text``, and that must end the loop
    rather than propagate — the disconnect is the ordinary way this route
    ends, not an error.
    """
    try:
        await ws.send_text(json.dumps(frame))
        return True
    except (WebSocketDisconnect, RuntimeError):
        return False


async def _poll_events(
    ws: WebSocket,
    home_path,
    conversation_id: str,
    record: dict[str, Any],
    sent: set[str],
    announced_terminal: bool,
) -> None:
    """Push every not-yet-sent event, then the terminal frame on each transition.

    The socket stays OPEN across a terminal status, and that is the whole
    reason it is not closed here. Closing a finished conversation is the
    obvious reading of "send the terminal frame, then close" — and it is
    wrong: the canvas opens this socket for EVERY conversation the user
    clicks, including ones that finished days ago, so a close-on-terminal
    rule paints "Disconnected" over the entire history. Worse, a finished
    conversation is resumable — the user can send another message and the
    run starts moving again — and a closed socket would have to be
    re-opened mid-turn, exactly when the events matter most.

    So the terminal frame is a *signal*, not a shutdown: announced once per
    transition, and re-armed if the run starts moving again.
    """
    deadline = time.monotonic() + _MAX_LIFETIME_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(_POLL_SECONDS)
        record = await asyncio.to_thread(_reload, home_path, conversation_id, record)
        events, status = await asyncio.to_thread(_project, home_path, record)
        fresh = [e for e in events if str(e.get("id")) not in sent]
        for event in fresh:
            if not await _send(ws, event):
                return
            sent.add(str(event.get("id")))
        if _is_terminal(status):
            if not announced_terminal:
                if not await _send(
                    ws,
                    _state_event(
                        f"{conversation_id}-state-final", "execution_status", status
                    ),
                ):
                    return
                announced_terminal = True
        else:
            # The run resumed (a follow-up message restarted it), so the
            # next terminal transition is a new fact the client needs.
            announced_terminal = False


async def _drain_inbound(ws: WebSocket, home_path, conversation_id: str) -> None:
    """Read client frames until the peer goes away.

    Two kinds arrive here. The canvas's auth handshake is
    ``{"type": "auth", "session_api_key": …}`` — a *control* frame, and the
    ``"type"`` key is what marks it: treating it as a user message would 400
    on an empty body and paint an error banner at the user on every connect.
    Everything else is a ``SendMessageRequest`` with ``run: true`` and goes
    straight to the REST handler, so a message sent over the socket and one
    sent over ``POST /events`` cannot take different paths.
    """
    while True:
        raw = await ws.receive_text()
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(payload, dict) or "type" in payload:
            continue
        try:
            await asyncio.to_thread(
                send_conversation_event, payload, conversation_id, home_path
            )
        except HTTPException as exc:
            await _send(
                ws,
                _error_event(
                    f"{conversation_id}-ws-error-{time.time_ns()}",
                    str(exc.status_code),
                    str(exc.detail),
                ),
            )
        except Exception as exc:  # noqa: BLE001 — surfaced, never swallowed
            await _send(
                ws,
                _error_event(
                    f"{conversation_id}-ws-error-{time.time_ns()}",
                    "ws_send_failed",
                    str(exc),
                ),
            )


@router.websocket("/sockets/events/{conversation_id}")
async def conversation_events_socket(
    ws: WebSocket,
    conversation_id: str,
    home: str | None = Query(default=None),
    resend_mode: str = Query(default="all"),
    after_timestamp: str | None = Query(default=None),
    resend_all: bool = Query(default=False),
    session_api_key: str | None = Query(default=None),
) -> None:
    """Stream a conversation's events, replaying the backlog first.

    ``session_api_key`` is accepted and ignored: these routes are deliberately
    tokenless (see ``agent_server``'s module docstring), and the canvas sends
    the key as a query param on every socket. Rejecting it would break the
    local posture the shim was built around.
    """
    del session_api_key, resend_all
    await ws.accept()

    if not _safe_conversation_id(conversation_id):
        await ws.close(code=_CLOSE_NORMAL)
        return
    home_path = get_home_lenient(None, home)
    record = await asyncio.to_thread(_load_conversation, home_path, conversation_id)
    if record is None:
        # Unknown conversation: close normally so the canvas shows its empty
        # state instead of hammering a reconnect loop against a 404.
        await ws.close(code=_CLOSE_NORMAL)
        return

    sent: set[str] = set()
    try:
        events, status = await asyncio.to_thread(_project, home_path, record)

        # Replay. `since` is the reconnect path: the client already holds
        # everything up to `after_timestamp`, so anything at or before it is
        # dropped. Same-second events are NOT lost to the strict comparison —
        # they are simply left unsent here and picked up by the poll loop
        # below, which is id-based and therefore exact.
        if resend_mode != "since" or not after_timestamp:
            replay = events
        else:
            replay = [e for e in events if str(e.get("timestamp") or "") > after_timestamp]

        # Handshake: the client renders execution status off a `full_state`
        # frame, and this one makes the chip correct before any event lands.
        if not await _send(ws, _state_event(f"{conversation_id}-state-init", "full_state", {"execution_status": status})):
            return
        for event in replay:
            if not await _send(ws, event):
                return
            sent.add(str(event.get("id")))

        announced_terminal = False
        if _is_terminal(status):
            if not await _send(
                ws,
                _state_event(
                    f"{conversation_id}-state-final", "execution_status", status
                ),
            ):
                return
            announced_terminal = True

        # Push and pull run concurrently: the poll task owns outbound frames
        # (including the terminal signal), this coroutine owns inbound ones.
        # Serialising them would mean a quiet conversation never notices a
        # dead peer, and an active one delays every user message by a tick.
        poll = asyncio.create_task(
            _poll_events(ws, home_path, conversation_id, record, sent, announced_terminal)
        )
        try:
            await _drain_inbound(ws, home_path, conversation_id)
        finally:
            poll.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll
    except WebSocketDisconnect:
        return
    except Exception:
        # Anything unexpected closes as "our fault" so the client retries
        # rather than sitting on a dead socket that looks connected.
        await ws.close(code=_CLOSE_FAILED)


@router.websocket("/sockets/bash-events")
async def bash_events_socket(ws: WebSocket, session_api_key: str | None = Query(default=None)) -> None:
    """Terminal event stream — accepted, held open, intentionally inert.

    The canvas pairs this with ``POST /api/bash/execute_bash_command``, which
    the shim does not serve, so there is nothing to forward yet. Holding the
    socket open is the honest middle: the terminal's connection chip reflects
    "connected, no output" instead of an error the user cannot resolve.
    """
    del session_api_key
    await ws.accept()
    try:
        while True:
            # Drain anything the client sends (currently only an auth frame)
            # so the socket stays healthy; nothing is echoed back.
            await ws.receive_text()
    except WebSocketDisconnect:
        return
    except Exception:
        await ws.close(code=_CLOSE_FAILED)
