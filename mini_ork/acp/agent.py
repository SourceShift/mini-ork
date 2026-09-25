"""ACP agent binding one session id to one run id via the launch seam.

Slice 0 of the Zed engineer surface (2026-09-20): Zed registers mini-ork as a
custom ACP agent (``mini-ork acp``) and opens one session per run. Because
``mini_ork.web.control.launch_run`` already validates any client-mintable run
id through ``_is_safe_token``, the session id and the run id can be the *same*
token — no session→run mapping table and no new backend plumbing.

``session/new`` mints that id (or honours a client-minted ``_meta.run_id``),
binds it to the session's ``cwd`` and recipe, and does not launch anything;
``session/prompt`` launches the detached run through the existing seam, then
*awaits* the run's terminal status, projecting read-model state (node lifecycle
→ tool-call updates, llm_calls → usage, terminal status → agent message) onto
the ACP wire via the stored connection. Each node transition is projected
exactly once (D1). ``session/cancel`` soft-stops the run and escalates to a hard
kill after a grace window. stdout is the ACP wire — this module never prints to
it.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Callable

from acp.schema import (
    AgentMessageChunk,
    Cost,
    Implementation,
    InitializeResponse,
    NewSessionResponse,
    PromptResponse,
    StopReason,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    UsageUpdate,
)

from mini_ork.web.control import _is_safe_token

PROTOCOL_VERSION = 1

# Default recipe used when the client does not name one; override per-process
# via MO_ACP_RECIPE. launch_run validates the token, so an env value that is
# not a safe slug is rejected there rather than injected.
DEFAULT_RECIPE = "code-fix"

# Terminal task_run statuses. Mirrors mini_ork.web.routes.run_detail.TERMINAL_STATUSES
# but duplicated here so this module does not import the FastAPI route graph.
TERMINAL_STATUSES = frozenset({"published", "rolled_back", "failed"})

# Poll cadence while a detached run is awaited, and the stop→kill grace window.
_POLL_INTERVAL_S = float(os.environ.get("MO_ACP_POLL_SECONDS", "2.0"))
_CANCEL_ESCALATE_S = float(os.environ.get("MO_ACP_CANCEL_GRACE", "3.0"))

# Reported context-window size ("size" in UsageUpdate). mini-ork has no per-call
# context column in llm_calls, so the projection reports a fixed window.
DEFAULT_CONTEXT_SIZE = 200_000


def mint_run_id() -> str:
    """Mint a client-mintable run id matching ``control.launch_run``'s shape."""
    return f"run-{int(time.time())}-{secrets.token_hex(3)}"


def _extract_prompt_text(prompt: list[Any]) -> str:
    """Flatten the text content blocks of an ACP prompt into one string."""
    chunks: list[str] = []
    for block in prompt or []:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text.strip():
            chunks.append(text)
    return "\n".join(chunks)


class MiniOrkAcpAgent:
    """Stdio ACP agent: one session == one run id.

    ``session/new`` mints or accepts a run id and binds it to the session ``cwd``
    and recipe; ``session/prompt`` launches that run detached via
    ``control.launch_run`` and awaits its terminal status while projecting
    read-model state onto the wire; ``session/cancel`` stops the run (escalating
    to a kill). Every side-effecting seam (launcher, reader, stopper, killer) is
    injectable so hermetic tests never spawn ``bin/mini-ork`` or touch a real
    state.db.
    """

    def __init__(
        self,
        *,
        home: str | Path | None = None,
        launcher: Callable[[str, str], dict[str, Any]] | None = None,
        reader: Callable[[str], dict[str, Any]] | None = None,
        stopper: Callable[[str], dict[str, Any]] | None = None,
        killer: Callable[[str], dict[str, Any]] | None = None,
        recipe: str | None = None,
        poll_interval: float | None = None,
        cancel_grace: float | None = None,
    ) -> None:
        # launcher: callable(run_id, kickoff_text) -> dict; reader: callable(run_id)
        # -> {"status", "events", "llm_calls"}; stopper/killer: callable(run_id) -> dict.
        self._home = str(home) if home else None
        self._launcher = launcher
        self._reader = reader
        self._stopper = stopper
        self._killer = killer
        self._recipe = recipe or os.environ.get("MO_ACP_RECIPE") or DEFAULT_RECIPE
        self._poll_interval = _POLL_INTERVAL_S if poll_interval is None else float(poll_interval)
        self._cancel_grace = _CANCEL_ESCALATE_S if cancel_grace is None else float(cancel_grace)
        # The AgentSideConnection handed to on_connect; session_update pushes
        # projected read-model updates back to the ACP client.
        self._conn: Any | None = None
        # session id → cwd binding (the "one session == one run" anchor).
        self._sessions: dict[str, str] = {}
        # session id → recipe (D2: honoured from the client's _meta).
        self._recipes: dict[str, str] = {}
        # session id → set of already-emitted node transitions (D1: emit once).
        self._emitted: dict[str, set[str]] = {}
        self._cancelled: set[str] = set()
        self._launch_count = 0

    @property
    def launch_count(self) -> int:
        """Number of run launches this agent has performed (observable)."""
        return self._launch_count

    def on_connect(self, conn: Any) -> None:
        """Store the AgentSideConnection so projections can reach the client."""
        self._conn = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any = None,
        client_info: Any = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        del protocol_version, client_capabilities, client_info, kwargs
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_info=Implementation(name="mini-ork-acp", version="0.8.0"),
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        del additional_directories, mcp_servers
        # The ACP router merges the request's _meta dict into kwargs, so a
        # client-minted run_id / recipe arrive here as plain keyword arguments.
        # Honour a client-minted run_id only when it is a safe token; otherwise
        # mint one in launch_run's shape. The session id IS the run id.
        requested = str(kwargs.get("run_id") or "").strip()
        run_id = requested if requested and _is_safe_token(requested) else mint_run_id()
        recipe = str(kwargs.get("recipe") or "").strip() or self._recipe
        self._sessions[run_id] = cwd
        self._recipes[run_id] = recipe
        return NewSessionResponse(session_id=run_id, field_meta={"run_id": run_id})

    async def prompt(
        self,
        session_id: str,
        prompt: list[Any],
        **kwargs: Any,
    ) -> PromptResponse:
        del kwargs
        if not _is_safe_token(session_id):
            return PromptResponse(stop_reason="refusal")
        self._launch_count += 1
        launcher = self._launcher or self._launch
        result = launcher(session_id, _extract_prompt_text(prompt))
        if isinstance(result, dict) and result.get("ok") is False:
            return PromptResponse(stop_reason="refusal")
        # The ACP turn ends only when the detached run reaches a terminal
        # status; a mid-run cancel (concurrent notification) returns 'cancelled'.
        return PromptResponse(stop_reason=await self._await_terminal(session_id))

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        del kwargs
        self._cancelled.add(session_id)
        stopper = self._stopper or self._stop
        stopper(session_id)  # soft stop first (dispatcher bails before next node)
        if self._cancel_grace and self._cancel_grace > 0:
            await asyncio.sleep(self._cancel_grace)
            reader = self._reader or self._read_snapshot
            try:
                status = (reader(session_id) or {}).get("status")
            except Exception:
                status = None
            if status not in TERMINAL_STATUSES:
                killer = self._killer or self._kill
                killer(session_id)  # hard-kill escalation

    # ── read-model projection ────────────────────────────────────────────────

    def _build_event_updates(self, events: list[dict[str, Any]] | None) -> list[Any]:
        """Project node lifecycle events into ToolCallStart/ToolCallProgress.

        Pure projection: every event in the batch becomes an update. Callers
        that poll repeatedly must pre-filter to *new* events (see
        ``_project_snapshot``) so each transition is emitted exactly once.
        """
        updates: list[Any] = []
        for ev in events or []:
            payload = ev.get("payload_json") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (json.JSONDecodeError, TypeError):
                    payload = {}
            node_id = str(payload.get("node_id") or ev.get("node_id") or "node")
            title = str(payload.get("node_type") or ev.get("node_type") or "node")
            if ev.get("event_type") == "node_start":
                updates.append(
                    ToolCallStart(
                        session_update="tool_call",
                        tool_call_id=node_id,
                        title=title,
                        status="in_progress",
                    )
                )
            elif ev.get("event_type") == "node_end":
                updates.append(
                    ToolCallProgress(
                        session_update="tool_call_update",
                        tool_call_id=node_id,
                        status="completed",
                    )
                )
        return updates

    def _event_transition_key(self, ev: dict[str, Any]) -> str | None:
        """Dedup key for one node lifecycle event, or None for non-node events.

        A ``node_start``/``node_end`` pair for the same ``node_id`` is one
        node's life; tracking ``node_id`` + transition lets the projector skip
        a transition it has already emitted (D1).
        """
        event_type = ev.get("event_type")
        if event_type not in ("node_start", "node_end"):
            return None
        payload = ev.get("payload_json") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                payload = {}
        node_id = str(payload.get("node_id") or ev.get("node_id") or "node")
        return f"{node_id}:{event_type.removeprefix('node_')}"

    def _build_usage_update(self, llm_calls: list[dict[str, Any]] | None) -> UsageUpdate:
        """Project llm_calls into a cumulative UsageUpdate (cost in USD)."""
        calls = list(llm_calls or [])
        cost = sum(float(c.get("cost_usd") or 0.0) for c in calls)
        used = sum(int(c.get("total_tokens") or 0) for c in calls)
        return UsageUpdate(
            session_update="usage_update",
            used=used,
            size=DEFAULT_CONTEXT_SIZE,
            cost=Cost(amount=cost, currency="USD"),
        )

    def _build_terminal_message(self, status: str) -> AgentMessageChunk:
        return AgentMessageChunk(
            session_update="agent_message_chunk",
            content=TextContentBlock(type="text", text=f"mini-ork run finished: {status}"),
        )

    async def _emit(self, session_id: str, update: Any) -> None:
        if self._conn is not None:
            await self._conn.session_update(session_id, update)

    async def _project_snapshot(self, session_id: str, snapshot: dict[str, Any]) -> None:
        emitted = self._emitted.setdefault(session_id, set())
        new_events: list[dict[str, Any]] = []
        for ev in snapshot.get("events") or []:
            key = self._event_transition_key(ev)
            if key is None or key in emitted:
                continue
            emitted.add(key)
            new_events.append(ev)
        updates = self._build_event_updates(new_events)
        updates.append(self._build_usage_update(snapshot.get("llm_calls")))
        status = snapshot.get("status")
        if status in TERMINAL_STATUSES:
            updates.append(self._build_terminal_message(status))
        for update in updates:
            await self._emit(session_id, update)

    async def _await_terminal(self, session_id: str) -> StopReason:
        reader = self._reader or self._read_snapshot
        while True:
            if session_id in self._cancelled:
                return "cancelled"
            snapshot = reader(session_id) or {"status": None, "events": [], "llm_calls": []}
            await self._project_snapshot(session_id, snapshot)
            if snapshot.get("status") in TERMINAL_STATUSES:
                return "end_turn"
            await asyncio.sleep(self._poll_interval)

    # ── launch/read/stop seams (deferred imports keep the module light) ──────

    def _resolve_home(self) -> Path:
        return Path(
            self._home
            or os.environ.get("MINI_ORK_HOME")
            or os.path.join(os.getcwd(), ".mini-ork")
        )

    def _launch(self, run_id: str, kickoff_text: str) -> dict[str, Any]:
        """Detached launch via the existing seam; the run id is the session id."""
        from mini_ork.web.control import launch_run

        cwd = self._sessions.get(run_id)
        recipe = self._recipes.get(run_id) or self._recipe
        extra_env = {"MO_TARGET_CWD": cwd} if cwd else None
        return launch_run(
            self._resolve_home(),
            recipe,
            kickoff_text,
            run_id=run_id,
            extra_env=extra_env,
        )

    def _read_snapshot(self, run_id: str) -> dict[str, Any]:
        """Read task_run status + node events + llm_calls from the read model."""
        from mini_ork.web.deps import db_for
        from mini_ork.web.repositories import RunDetailRepository

        repo = RunDetailRepository(db_for(self._resolve_home()))
        tr = repo.fetch_task_run_row(run_id)
        if not tr:
            return {"status": None, "events": [], "llm_calls": []}
        events = repo.fetch_node_lifecycle_events(run_id)
        llm_calls: list[dict[str, Any]] = []
        window = repo.fetch_trace_window(run_id)
        if window and window.get("trace_id"):
            llm_calls = repo.fetch_llm_calls_by_trace_id(window["trace_id"])
        if window and window.get("created_at"):
            upper = window.get("ended_at") or int(time.time())
            llm_calls.extend(
                repo.fetch_llm_calls_in_window(int(window["created_at"]), int(upper))
            )
        # The trace_id and time-window bridges can overlap; dedupe by row id.
        seen: set[Any] = set()
        deduped: list[dict[str, Any]] = []
        for row in llm_calls:
            rid = row.get("id")
            if rid in seen:
                continue
            seen.add(rid)
            deduped.append(row)
        return {"status": tr.get("status"), "events": events, "llm_calls": deduped}

    def _stop(self, run_id: str) -> dict[str, Any]:
        from mini_ork.web.control import stop_run
        from mini_ork.web.deps import db_for

        return stop_run(self._resolve_home(), db_for(self._resolve_home()), run_id)

    def _kill(self, run_id: str) -> dict[str, Any]:
        from mini_ork.web.control import kill_run
        from mini_ork.web.deps import db_for

        return kill_run(self._resolve_home(), db_for(self._resolve_home()), run_id)
