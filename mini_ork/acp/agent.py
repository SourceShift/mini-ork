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

from acp import RequestError
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    AgentThoughtChunk,
    ContentToolCallContent,
    Cost,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptResponse,
    SessionCapabilities,
    SessionInfo,
    SessionListCapabilities,
    StopReason,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    UsageUpdate,
    UserMessageChunk,
)

from mini_ork.acp import history
from mini_ork.acp.live import LiveTail, normalize
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
# Hard ceiling for "how long may the launcher take before we call the run
# refused". Without it, a launcher that exits before publishing a task_run row
# (bad interpreter, missing venv, ...) leaves _await_terminal polling forever.
_START_TIMEOUT_S = float(os.environ.get("MO_ACP_START_TIMEOUT_S", "300"))

# Reported context-window size ("size" in UsageUpdate). mini-ork has no per-call
# context column in llm_calls, so the projection reports a fixed window.
DEFAULT_CONTEXT_SIZE = 200_000

# Live sidecar replay cap (kickoff §3): ``session/load`` of a finished run
# replays at most the last N normalized events per node, so a long-lived
# node that produced thousands of chunks cannot blow up the load replay.
_LIVE_REPLAY_LIMIT = 50

# Live sidecar poll cap (kickoff §3): per-poll output is bounded so a chatty
# node cannot saturate the ACP wire in a single tick. The cap is a per-tick
# buffer bound — the byte offset always advances, so subsequent polls catch
# up rather than back-pressure forever.
_LIVE_POLL_LIMIT = 100

# Filename sanitization for the per-node live sidecar — matches the writer
# side in ``mini_ork.dispatch.providers._attach_isolation``. Slashes (a node
# id like ``planner/code-impact``) become underscores so the path stays under
# ``<run_dir>/agent-<safe>.live.jsonl``.
_LIVE_NODE_SAFE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz"
                                  "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                                  "0123456789._-")


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


def _tail_log(path: str | None, n: int = 20) -> str:
    """Last ``n`` lines of ``path`` for refusal-text bodies; never raises."""
    if not path:
        return "(no launch log)"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:]).rstrip("\n") or "(launch log empty)"
    except OSError:
        return "(launch log unreadable)"


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
        start_timeout: float | None = None,
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
        self._start_timeout_s = _START_TIMEOUT_S if start_timeout is None else float(start_timeout)
        # The AgentSideConnection handed to on_connect; session_update pushes
        # projected read-model updates back to the ACP client.
        self._conn: Any | None = None
        # session id → cwd binding (the "one session == one run" anchor).
        self._sessions: dict[str, str] = {}
        # session id → recipe (D2: honoured from the client's _meta).
        self._recipes: dict[str, str] = {}
        # session id → set of already-emitted node transitions (D1: emit once).
        self._emitted: dict[str, set[str]] = {}
        # session id → launcher result dict (carries pid / log_path so
        # _await_terminal can probe whether the launcher is still alive and
        # tail its log on refusal). Injected launchers may omit keys; missing
        # keys degrade to "unknown" rather than crash.
        self._launches: dict[str, dict[str, Any]] = {}
        self._cancelled: set[str] = set()
        # session id → follow task for an attached (loaded) run that has not
        # reached a terminal status yet (Z2 live follow).
        self._followers: dict[str, asyncio.Task] = {}
        # session ids opened via session/load (never launched by prompt).
        self._loaded: set[str] = set()
        # (session, node) pairs whose agent already streamed text — a final
        # ``result`` event is shown only for nodes that streamed nothing.
        self._text_seen: set[tuple[str, str]] = set()
        # session id → node_id → LiveTail (Z3 live projection). Constructed
        # lazily on first node_start; held until session end.
        self._tails: dict[str, dict[str, LiveTail]] = {}
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
            agent_info=Implementation(name="mini-ork-acp", version="0.9.0"),
            agent_capabilities=AgentCapabilities(
                load_session=True,
                session_capabilities=SessionCapabilities(list=SessionListCapabilities()),
            ),
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

    async def list_sessions(
        self, cwd: str | None = None, cursor: str | None = None, **kwargs: Any
    ) -> ListSessionsResponse:
        del kwargs
        # A cwd names a project: list that project's runs or nothing. Falling back
        # to the agent's default home would show another project's runs for a
        # project that has no .mini-ork.
        if cwd:
            home = Path(cwd) / ".mini-ork"
            if not home.is_dir():
                return ListSessionsResponse(sessions=[])
        else:
            home = self._resolve_home()
        if not home.exists():
            return ListSessionsResponse(sessions=[])
        try:
            offset = int(cursor) if cursor else 0
        except (TypeError, ValueError):
            offset = 0
        if offset < 0:
            offset = 0
        rows, next_offset = history.list_runs(home, limit=50, offset=offset)
        sessions = [
            SessionInfo(
                session_id=row["run_id"],
                cwd=str(home.resolve().parent),
                title=row.get("title"),
                updated_at=row.get("updated_at"),
                field_meta={
                    "status": row.get("status"),
                    "recipe": row.get("recipe"),
                    "cost_usd": row.get("cost_usd"),
                },
            )
            for row in rows
        ]
        return ListSessionsResponse(
            sessions=sessions,
            next_cursor=str(next_offset) if next_offset is not None else None,
        )

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[Any] | None = None,
        additional_directories: list[str] | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse | None:
        del mcp_servers, additional_directories, kwargs
        if not _is_safe_token(session_id):
            raise RequestError.invalid_params({"message": f"unsafe session id: {session_id!r}"})
        # Bind the session to its project cwd (the session id IS the run id);
        # reset emitted transitions so replay starts clean.
        self._sessions[session_id] = cwd
        self._emitted.pop(session_id, None)
        home = self._home_for(session_id)
        snapshot = history.read_snapshot(home, session_id)
        if snapshot.get("status") is None:
            raise RequestError.invalid_params({"message": f"no mini-ork run {session_id}"})
        self._loaded.add(session_id)
        kickoff = history.kickoff_text(home, session_id)
        if kickoff:
            await self._emit(
                session_id,
                UserMessageChunk(
                    session_update="user_message_chunk",
                    content=TextContentBlock(type="text", text=kickoff),
                ),
            )
        await self._project_snapshot(session_id, snapshot, live=False)
        # Z3 live replay (kickoff §3): for finished runs, replay the last
        # ``_LIVE_REPLAY_LIMIT`` normalized events per node. Running runs
        # delegate to ``_follow`` which drains per poll with a clean offset.
        if snapshot.get("status") in TERMINAL_STATUSES:
            started = self._started_node_ids(snapshot)
            if started:
                await self._replay_live_for(session_id, sorted(started))
        if snapshot.get("status") not in TERMINAL_STATUSES:
            self._followers[session_id] = asyncio.create_task(self._follow(session_id))
        return LoadSessionResponse()

    async def prompt(
        self,
        session_id: str,
        prompt: list[Any],
        **kwargs: Any,
    ) -> PromptResponse:
        del kwargs
        if not _is_safe_token(session_id):
            return PromptResponse(stop_reason="refusal")
        if session_id in self._loaded:
            # Loaded (attached) sessions never launch: the run already exists.
            # Stream status context and end the turn immediately.
            snapshot = (self._reader or self._read_snapshot)(session_id) or {
                "status": None,
                "events": [],
                "llm_calls": [],
            }
            status = snapshot.get("status")
            if status in TERMINAL_STATUSES:
                await self._emit(
                    session_id,
                    self._build_refusal_message(
                        f"This thread shows a finished run ({status}). "
                        "Start a new thread to run again."
                    ),
                )
            else:
                await self._emit(
                    session_id,
                    self._build_refusal_message(
                        f"Attached to running mini-ork run {session_id}; "
                        "updates stream here. Start a new thread to launch another run."
                    ),
                )
            return PromptResponse(stop_reason="end_turn")
        self._launch_count += 1
        launcher = self._launcher or self._launch
        result = launcher(session_id, _extract_prompt_text(prompt))
        # Stash whatever the launcher returned so _await_terminal can detect a
        # dead launcher (pid reaped) and tail its log on refusal. Injected
        # launchers in tests may omit pid / log_path; treat as "unknown".
        self._launches[session_id] = result if isinstance(result, dict) else {}
        if isinstance(result, dict) and result.get("ok") is False:
            return PromptResponse(stop_reason="refusal")
        # The ACP turn ends only when the detached run reaches a terminal
        # status; a mid-run cancel (concurrent notification) returns 'cancelled'.
        return PromptResponse(stop_reason=await self._await_terminal(session_id))

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        del kwargs
        self._cancelled.add(session_id)
        follower = self._followers.get(session_id)
        if follower is not None and not follower.done():
            follower.cancel()
        if session_id in self._loaded:
            # Attached session: soft-stop only when the run is still running;
            # a finished run is a no-op (there is nothing to cancel).
            snapshot = (self._reader or self._read_snapshot)(session_id) or {
                "status": None,
                "events": [],
                "llm_calls": [],
            }
            status = snapshot.get("status")
            if status is not None and status not in TERMINAL_STATUSES:
                stopper = self._stopper or self._stop
                stopper(session_id)
            return
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

    def _build_refusal_message(self, text: str) -> AgentMessageChunk:
        """Same shape as ``_build_terminal_message``; emitted when the turn
        ends in ``refusal`` (launcher died or start timeout exceeded).
        """
        return AgentMessageChunk(
            session_update="agent_message_chunk",
            content=TextContentBlock(type="text", text=text),
        )

    # ── live sidecar projection (Z3) ─────────────────────────────────────────

    @staticmethod
    def _safe_node_id(node_id: str) -> str:
        """Filenames-safe form of ``node_id`` (slashes and odd chars → ``_``).

        Matches the writer-side sanitization in
        ``mini_ork.dispatch.providers._attach_isolation`` so the writer and
        the tailer agree on the path. A node id like ``planner/code-impact``
        resolves to ``agent-planner_code-impact.live.jsonl``.
        """
        return "".join(c if c in _LIVE_NODE_SAFE_CHARS else "_" for c in node_id)

    def _ensure_tail(self, session_id: str, node_id: str) -> LiveTail:
        """Lazily build the ``LiveTail`` for ``(session_id, node_id)``.

        The path resolves to ``<home>/runs/<session_id>/agent-<safe>.live.jsonl``
        — the same path the dispatch writer appends to
        (see ``_attach_isolation`` and ``web/routes/node_live.LIVE_FILE_NAME``).
        The tail is cached for the session's lifetime so the byte offset
        accumulates across polls and a chatty node does not re-read the
        file from offset 0 on every poll.
        """
        bucket = self._tails.setdefault(session_id, {})
        tail = bucket.get(node_id)
        if tail is None:
            run_dir = self._home_for(session_id) / "runs" / session_id
            tail = LiveTail(run_dir / f"agent-{self._safe_node_id(node_id)}.live.jsonl")
            bucket[node_id] = tail
        return tail

    def _started_nodes(self, session_id: str) -> set[str]:
        """Node ids whose ``node_start`` has already been emitted (D1 marker).

        ``_emitted[session_id]`` stores lifecycle keys of the form
        ``"<node_id>:start"``; peeling the suffix yields the set of nodes the
        client has been opened for. Live chunks for nodes that haven't yet
        been ToolCallStarted would land on the wire before the corresponding
        ToolCallStart — silent protocol breakage. ``_drain_live_for`` only
        touches nodes returned here.
        """
        out: set[str] = set()
        for key in self._emitted.get(session_id, set()):
            if key.endswith(":start"):
                out.add(key[: -len(":start")])
        return out

    @staticmethod
    def _started_node_ids(snapshot: dict[str, Any]) -> set[str]:
        """Node ids whose ``node_start`` appears in ``snapshot["events"]``.

        Used by ``load_session`` to seed the live replay for finished runs:
        we replay one batch per started node, capped at ``_LIVE_REPLAY_LIMIT``
        events each. Parses ``payload_json`` defensively (string or dict).
        """
        out: set[str] = set()
        for ev in snapshot.get("events") or []:
            if ev.get("event_type") != "node_start":
                continue
            payload = ev.get("payload_json")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (json.JSONDecodeError, TypeError):
                    payload = {}
            if not isinstance(payload, dict):
                payload = {}
            node_id = str(payload.get("node_id") or ev.get("node_id") or "node")
            out.add(node_id)
        return out

    async def _drain_live_for(
        self, session_id: str, *, limit: int = _LIVE_POLL_LIMIT
    ) -> None:
        """Read new live records for every started node and emit each chunk.

        Per poll, each node contributes at most ``limit`` events (kickoff §3).
        The byte offset advances regardless of the cap, so a chatty node
        drains across subsequent polls instead of back-pressuring the loop.

        The four ``LiveEvent.kind`` values map to two ACP update types:

        * ``thought`` → ``AgentThoughtChunk`` (agent's own reasoning);
        * ``text`` / ``tool`` / ``tool_output`` → ``ToolCallProgress`` chunks
          with a single ``TextContentBlock`` carrying the text.

        Each chunk is appended to the existing ``ToolCallProgress`` stream
        for the same ``tool_call_id`` — the SDK accumulates, never replaces.
        """
        started = self._started_nodes(session_id)
        if not started:
            return
        for node_id in started:
            tail = self._ensure_tail(session_id, node_id)
            records = tail.read_new()
            if not records:
                continue
            events: list[Any] = []
            for rec in records:
                for ev in self._final_answer_once(session_id, node_id, normalize(rec)):
                    if ev.kind == "thought":
                        events.append(
                            AgentThoughtChunk(
                                session_update="agent_thought_chunk",
                                content=TextContentBlock(
                                    type="text",
                                    text=f"[{node_id}] {ev.text}",
                                ),
                            )
                        )
                    else:
                        events.append(
                            ToolCallProgress(
                                session_update="tool_call_update",
                                tool_call_id=node_id,
                                content=[
                                    ContentToolCallContent(
                                        type="content",
                                        content=TextContentBlock(
                                            type="text", text=ev.text
                                        ),
                                    )
                                ],
                            )
                        )
            # Per-poll cap (kickoff §3): the byte offset already advanced
            # past these records, so subsequent polls keep making progress.
            for update in events[:limit]:
                await self._emit(session_id, update)

    def _final_answer_once(self, session_id: str, node_id: str, events: list[Any]) -> list[Any]:
        """Pass live events through, keeping a node's final ``result`` only when
        that node streamed no text (a non-streaming lane writes nothing but the
        final object; a streaming lane would otherwise show its answer twice)."""
        key = (session_id, node_id)
        out: list[Any] = []
        for ev in events:
            if ev.kind == "text":
                self._text_seen.add(key)
            elif ev.kind == "result":
                if key in self._text_seen:
                    continue
                self._text_seen.add(key)
            out.append(ev)
        return out

    async def _replay_live_for(self, session_id: str, node_ids: list[str]) -> None:
        """Replay the last ``_LIVE_REPLAY_LIMIT`` normalized events per node.

        Called from ``load_session`` for FINISHED runs (kickoff §3): the
        operator attaches to a past run and gets a final burst of what each
        agent actually said, capped so a long-lived node cannot blow up the
        load replay. Each tail is opened from offset 0 and discarded at the
        end so a later ``_follow`` starts with a clean offset — there is no
        double-emission.
        """
        for node_id in node_ids:
            tail = self._ensure_tail(session_id, node_id)
            records = tail.read_new()  # offset 0 on first use
            if not records:
                continue
            events: list[Any] = []
            for rec in records:
                for ev in self._final_answer_once(session_id, node_id, normalize(rec)):
                    if ev.kind == "thought":
                        events.append(
                            AgentThoughtChunk(
                                session_update="agent_thought_chunk",
                                content=TextContentBlock(
                                    type="text",
                                    text=f"[{node_id}] {ev.text}",
                                ),
                            )
                        )
                    else:
                        events.append(
                            ToolCallProgress(
                                session_update="tool_call_update",
                                tool_call_id=node_id,
                                content=[
                                    ContentToolCallContent(
                                        type="content",
                                        content=TextContentBlock(
                                            type="text", text=ev.text
                                        ),
                                    )
                                ],
                            )
                        )
            tail.offset = 0  # future polls start at offset 0, not replay
            # Tail is the last ``_LIVE_REPLAY_LIMIT`` events.
            for update in events[-_LIVE_REPLAY_LIMIT:]:
                await self._emit(session_id, update)

    def _is_pid_gone(self, pid: int) -> bool:
        """True when ``pid`` is no longer running.

        Mirrors mini_ork/web/routes/pty.py:195-206: a non-(0,0) waitpid return
        means the child was reaped; a ChildProcessError means it isn't ours (or
        already gone). Any other OSError is treated as "still running" so a flaky
        probe doesn't tip the agent into a false refusal.
        """
        try:
            result = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return True
        except OSError:
            return False
        return result != (0, 0)

    async def _emit(self, session_id: str, update: Any) -> None:
        if self._conn is not None:
            await self._conn.session_update(session_id, update)

    async def _project_snapshot(
        self, session_id: str, snapshot: dict[str, Any], *, live: bool = True
    ) -> None:
        emitted = self._emitted.setdefault(session_id, set())
        new_events: list[dict[str, Any]] = []
        for ev in snapshot.get("events") or []:
            key = self._event_transition_key(ev)
            if key is None or key in emitted:
                continue
            emitted.add(key)
            new_events.append(ev)
        # Live drain (Z3): any node whose node_start has already been emitted
        # is followed here. Drain happens BEFORE the lifecycle events so a
        # text chunk follows its tool call, and BEFORE the terminal message
        # so nothing written at the very end is lost (kickoff §3).
        if live:
            await self._drain_live_for(session_id)
        updates = self._build_event_updates(new_events)
        updates.append(self._build_usage_update(snapshot.get("llm_calls")))
        status = snapshot.get("status")
        if status in TERMINAL_STATUSES:
            updates.append(self._build_terminal_message(status))
        for update in updates:
            await self._emit(session_id, update)

    async def _await_terminal(self, session_id: str) -> StopReason:
        reader = self._reader or self._read_snapshot
        # Start-timeout clock runs once per turn (kicks off at the first poll).
        start_deadline = time.monotonic() + self._start_timeout_s
        pid_gone_observed = False
        while True:
            if session_id in self._cancelled:
                return "cancelled"
            snapshot = reader(session_id) or {"status": None, "events": [], "llm_calls": []}
            await self._project_snapshot(session_id, snapshot)
            if snapshot.get("status") is None:
                # The run hasn't produced a task_runs row yet; look for an
                # early death of the launcher (shebang interpreter bug, missing
                # venv, ...) and for a hard start-timeout.
                launch_info = self._launches.get(session_id) or {}
                pid = launch_info.get("pid")
                log_path = launch_info.get("log_path")
                # 1. Probe the detached launcher process. A quick exit means the
                #    run never produced a row; one extra read confirms.
                if pid is not None and not pid_gone_observed and self._is_pid_gone(pid):
                    pid_gone_observed = True
                    confirm = reader(session_id) or {"status": None, "events": [], "llm_calls": []}
                    await self._project_snapshot(session_id, confirm)
                    if confirm.get("status") is None:
                        await self._emit(
                            session_id,
                            self._build_refusal_message(
                                "mini-ork run failed to start:\n" + _tail_log(log_path, 20)
                            ),
                        )
                        return "refusal"
                # 2. Start-timeout fallback (independent of pid probe).
                if not pid_gone_observed and time.monotonic() >= start_deadline:
                    await self._emit(
                        session_id,
                        self._build_refusal_message(
                            f"mini-ork run did not start within {self._start_timeout_s:g}s\n"
                            + _tail_log(log_path, 20)
                        ),
                    )
                    return "refusal"
            if snapshot.get("status") in TERMINAL_STATUSES:
                return "end_turn"
            await asyncio.sleep(self._poll_interval)

    async def _follow(self, session_id: str) -> None:
        """Poll an attached (loaded) run and project its read-model state.

        Mirrors ``_await_terminal`` minus the launcher probe and start-timeout
        refusal: there is no launch to wait for, so a missing row just keeps
        polling until the run reaches a terminal status or the session is
        cancelled. The loop ends without emitting a terminal message on
        cancel — ``_project_snapshot`` already emitted everything new.
        """
        reader = self._reader or self._read_snapshot
        while session_id not in self._cancelled:
            snapshot = reader(session_id) or {"status": None, "events": [], "llm_calls": []}
            await self._project_snapshot(session_id, snapshot)
            if snapshot.get("status") in TERMINAL_STATUSES:
                break
            await asyncio.sleep(self._poll_interval)

    # ── launch/read/stop seams (deferred imports keep the module light) ──────

    def _resolve_home(self) -> Path:
        return Path(
            self._home
            or os.environ.get("MINI_ORK_HOME")
            or os.path.join(os.getcwd(), ".mini-ork")
        )

    def _home_for(self, session_id: str | None) -> Path:
        """Resolve the ``.mini-ork`` home for a session id.

        Step 1 — the session's bound cwd wins when ``<cwd>/.mini-ork`` is a
        directory (a CLI-started run in a different project resolves to the
        right home). Steps 2–4 are the no-session fallback: constructor home →
        ``MINI_ORK_HOME`` → ``<process cwd>/.mini-ork`` (``_resolve_home``).
        """
        cwd = self._sessions.get(session_id) if session_id is not None else None
        if cwd:
            candidate = Path(cwd) / ".mini-ork"
            if candidate.is_dir():
                return candidate
        return self._resolve_home()

    def _launch(self, run_id: str, kickoff_text: str) -> dict[str, Any]:
        """Detached launch via the existing seam; the run id is the session id."""
        from mini_ork.web.control import launch_run

        cwd = self._sessions.get(run_id)
        recipe = self._recipes.get(run_id) or self._recipe
        extra_env = {"MO_TARGET_CWD": cwd} if cwd else None
        return launch_run(
            self._home_for(run_id),
            recipe,
            kickoff_text,
            run_id=run_id,
            extra_env=extra_env,
        )

    def _read_snapshot(self, run_id: str) -> dict[str, Any]:
        """Read task_run status + node events + llm_calls from the read model."""
        return history.read_snapshot(self._home_for(run_id), run_id)

    def _stop(self, run_id: str) -> dict[str, Any]:
        from mini_ork.web.control import stop_run
        from mini_ork.web.deps import db_for

        home = self._home_for(run_id)
        return stop_run(home, db_for(home), run_id)

    def _kill(self, run_id: str) -> dict[str, Any]:
        from mini_ork.web.control import kill_run
        from mini_ork.web.deps import db_for

        home = self._home_for(run_id)
        return kill_run(home, db_for(home), run_id)
