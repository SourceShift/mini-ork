"""ACP agent binding one session id to one run id via the launch seam
OR a thread session to an orchestrator conversation.

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

Slice Z9c-1 (2026-10-03) layers thread sessions on top: ``session/new`` with
no client-minted ``run_id`` mints an ``orch-<epoch>-<hex>`` id and configures
the session with three Zed-rendered pickers (mode, model, recipe). ``prompt``
on a thread session routes to ``acp_orchestrator.harness.run_turn`` in
``orchestrate`` mode (the default) or launches a run + follows it in
``direct`` mode, with child runs from ``start_run`` tool_results projected
under prefixed tool ids so they never collide with the orchestrator's own.
The run-session path above is unchanged — run ids are still safe tokens and
the launcher / reader / stopper / killer seams still apply.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, cast

from acp import RequestError
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    AgentThoughtChunk,
    ConfigOptionUpdate,
    ContentToolCallContent,
    Cost,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptResponse,
    SessionCapabilities,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SessionInfo,
    SessionListCapabilities,
    SessionMode,
    SessionModeState,
    SetSessionConfigOptionResponse,
    SetSessionModeResponse,
    StopReason,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    UsageUpdate,
    UserMessageChunk,
)

from mini_ork.acp import history
from mini_ork.acp import orchestration as _orchestration
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


def _mint_thread_id() -> str:
    """Mint an ``orch-…`` session id for a thread (orchestrator) session.

    Same epoch+hex shape as :func:`mint_run_id` so ``_is_safe_token`` accepts
    it unchanged. The ``orch-`` prefix is the loader-vs-thread discriminator
    the agent keys on in ``prompt`` / ``cancel``.
    """
    return f"orch-{int(time.time())}-{secrets.token_hex(3)}"


# Per-session config a thread session carries alongside its cwd binding.
# Populated by ``new_session`` + ``set_config_option`` and consumed by
# ``prompt`` when routing the orchestrator vs direct paths.
_THREAD_CONFIG_KEYS = ("mode", "model", "recipe")

# Mode ids the kickoff mandates. Anything else is rejected by
# ``set_config_option`` as ``invalid_params``.
_MODE_ORCHESTRATE = "orchestrate"
_MODE_DIRECT = "direct"

# Slash command that triggers direct-mode behaviour for a single prompt
# regardless of the session's stored mode. The text after the prefix is
# the kickoff body passed to the launcher.
_SLASH_RUN_PREFIX = "/run "


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
    """Stdio ACP agent: thread sessions OR one-session-per-run.

    Thread sessions (Z9c-1): ``session/new`` with no client-minted ``run_id``
    mints an ``orch-<epoch>-<hex>`` id, binds it to the session ``cwd``, and
    configures three Zed-rendered pickers (mode, model, recipe). ``prompt``
    on a thread session runs the orchestrator (default ``orchestrate`` mode)
    or launches a run + follows it (``direct`` mode). Child runs spawned by
    ``start_run`` tool_results are projected under ``"<run_id>:"``-prefixed
    tool ids so two projections never collide.

    Run sessions (slice 0, unchanged): ``session/new`` honours a client-minted
    ``_meta.run_id`` (or mints one); ``session/prompt`` launches the run
    detached via ``control.launch_run`` and awaits its terminal status while
    projecting read-model state onto the wire; ``session/cancel`` stops the
    run (escalating to a kill). Every side-effecting seam (launcher, reader,
    stopper, killer, orchestrator_turn) is injectable so hermetic tests never
    spawn ``bin/mini-ork`` or touch a real state.db.
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
        orchestrator_turn: Callable[..., Awaitable[Any]] | None = None,
        default_mode: str | None = None,
        default_model: str | None = None,
        default_recipe: str | None = None,
    ) -> None:
        # launcher: callable(run_id, kickoff_text) -> dict; reader: callable(run_id)
        # -> {"status", "events", "llm_calls"}; stopper/killer: callable(run_id) -> dict.
        # orchestrator_turn: callable(lane, prompt, cwd, home, resume, on_event)
        # -> TurnResult; defaults to acp_orchestrator.harness.run_turn so the
        # CLI process spawns a real claude subprocess. Tests inject a fake.
        self._home = str(home) if home else None
        self._launcher = launcher
        self._reader = reader
        self._stopper = stopper
        self._killer = killer
        self._recipe = recipe or os.environ.get("MO_ACP_RECIPE") or DEFAULT_RECIPE
        self._poll_interval = _POLL_INTERVAL_S if poll_interval is None else float(poll_interval)
        self._cancel_grace = _CANCEL_ESCALATE_S if cancel_grace is None else float(cancel_grace)
        self._start_timeout_s = _START_TIMEOUT_S if start_timeout is None else float(start_timeout)
        # orchestrator_turn seam (Z9c-1). Defaults to a thin wrapper around
        # ``acp_orchestrator.harness.run_turn``; hermetic tests inject a fake
        # that calls ``on_event`` with canned stream-json blocks.
        self._orchestrator_turn = orchestrator_turn
        # Default thread-session config (env → ctor arg fallback). Per-session
        # overrides flow through ``set_config_option`` → ``_thread_config``.
        self._default_mode = (
            default_mode
            or os.environ.get("MO_ACP_DEFAULT_MODE")
            or _MODE_ORCHESTRATE
        )
        self._default_model = (
            default_model or os.environ.get("MO_ACP_DEFAULT_MODEL") or None
        )
        self._default_recipe = (
            default_recipe
            or os.environ.get("MO_ACP_RECIPE")
            or self._recipe
        )
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
        # Thread (orchestrator) session ids (Z9c-1). The prompt path
        # branches on membership: thread sessions route to orchestrate/direct;
        # everything else is a run session.
        self._thread_sessions: set[str] = set()
        # thread session id → {mode, model, recipe, claude_session_id}. The
        # first three are surfaced via config options; claude_session_id is
        # the resume token persisted across orchestrator turns.
        self._thread_config: dict[str, dict[str, str]] = {}
        # thread session id → running orchestrator turn task (Z9c-1 cancel
        # support); one per thread session. Cleared on turn end.
        self._orchestrator_tasks: dict[str, asyncio.Task] = {}
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
        # mint a thread (orchestrator) session — Z9c-1's primary new flow.
        requested = str(kwargs.get("run_id") or "").strip()
        if requested and _is_safe_token(requested):
            run_id = requested
            recipe = str(kwargs.get("recipe") or "").strip() or self._recipe
            self._sessions[run_id] = cwd
            self._recipes[run_id] = recipe
            return NewSessionResponse(session_id=run_id, field_meta={"run_id": run_id})
        # Thread session (Z9c-1). The session id is NOT a run id; it carries
        # the thread config (mode / model / recipe) plus the persisted claude
        # session id (set on first orchestrator turn). The three pickers are
        # Zed-rendered via ``category="model" | "mode"``.
        thread_id = _mint_thread_id()
        self._sessions[thread_id] = cwd
        self._thread_sessions.add(thread_id)
        self._thread_config[thread_id] = self._initial_thread_config(cwd)
        home = self._home_for(thread_id)
        config_options = self._build_config_options(thread_id, home)
        modes = self._build_session_modes(thread_id)
        return NewSessionResponse(
            session_id=thread_id,
            modes=modes,
            config_options=cast(Any, config_options),
            field_meta={"kind": "thread"},
        )

    # ── thread-session helpers (Z9c-1) ────────────────────────────────────────

    def _initial_thread_config(self, cwd: str) -> dict[str, str]:
        """Per-session defaults populated at ``new_session`` time.

        ``recipe`` falls back to the agent's default recipe; ``mode`` falls
        back to ``_default_mode``; ``model`` falls back to ``_default_model``
        (or the orchestrator's lane-map default for this session's home).
        """
        home = self._home_for_for_cwd(cwd)
        model = self._default_model or self._default_orchestrator_lane(home)
        return {
            "mode": self._default_mode,
            "model": model,
            "recipe": self._default_recipe,
            "claude_session_id": "",
        }

    def _home_for_for_cwd(self, cwd: str) -> Path:
        """Resolve ``.mini-ork`` for a raw cwd (without a session id)."""
        candidate = Path(cwd) / ".mini-ork"
        if candidate.is_dir():
            return candidate
        return self._resolve_home()

    def _default_orchestrator_lane(self, home: Path) -> str:
        """Pick the default lane for a thread session at ``home``.

        Tries ``acp_orchestrator.config.default_lane(home)`` first, then
        falls back to ``"opus"`` if the orchestrator package is unavailable
        (e.g. when the agent is constructed in a thin test harness that
        omits the orchestrator dependencies). The fallback keeps the picker
        functional — never raises.
        """
        try:
            from mini_ork.acp_orchestrator.config import default_lane
        except Exception:  # noqa: BLE001 — picker must never crash new_session
            return "opus"
        try:
            return default_lane(home)
        except Exception:  # noqa: BLE001 — picker must never crash new_session
            return "opus"

    def _build_config_options(
        self, session_id: str, home: Path
    ) -> list[SessionConfigOptionSelect]:
        """Materialise the three Zed-rendered pickers in kickoff order.

        Order: mode (category="mode"), model (category="model"), recipe (no
        category). Each option carries the current stored value as
        ``current_value``. Picker sources: ``acp_orchestrator.config`` for
        model lanes; ``web.recipes.list_recipes`` for recipes; the literal
        ``{"orchestrate", "direct"}`` for mode.
        """
        cfg = self._thread_config.get(session_id) or {}
        return [
            SessionConfigOptionSelect(
                type="select",
                id="mode",
                name="Mode",
                description="How each prompt in this thread is interpreted.",
                category="mode",
                current_value=str(cfg.get("mode") or _MODE_ORCHESTRATE),
                options=[
                    SessionConfigSelectOption(
                        value=_MODE_ORCHESTRATE,
                        name="Orchestrate",
                        description="Talk to the mini-ork orchestrator",
                    ),
                    SessionConfigSelectOption(
                        value=_MODE_DIRECT,
                        name="Direct run",
                        description="Each prompt is a run kickoff",
                    ),
                ],
            ),
            self._build_model_config_option(session_id, home),
            self._build_recipe_config_option(session_id),
        ]

    def _build_model_config_option(
        self, session_id: str, home: Path
    ) -> SessionConfigOptionSelect:
        cfg = self._thread_config.get(session_id) or {}
        lanes = self._list_orchestrator_lanes(home)
        current = str(cfg.get("model") or "")
        if current and not any(opt.value == current for opt in lanes):
            current = lanes[0].value if lanes else ""
        if not current and lanes:
            current = lanes[0].value
        return SessionConfigOptionSelect(
            type="select",
            id="model",
            name="Model",
            description="Lane the orchestrator drives this thread on.",
            category="model",
            current_value=current,
            options=lanes,
        )

    def _list_orchestrator_lanes(
        self, home: Path
    ) -> list[SessionConfigSelectOption]:
        """List ``acp_orchestrator.config.orchestrator_lanes()`` as picker rows.

        Defensive against missing orchestrator package or registry errors —
        a thread session must always be configurable, so any exception
        degrades to the single-opus fallback.
        """
        try:
            from mini_ork.acp_orchestrator.config import orchestrator_lanes
        except Exception:  # noqa: BLE001 — picker must never crash new_session
            return [SessionConfigSelectOption(value="opus", name="opus")]
        try:
            # ``orchestrator_lanes`` takes the ENGINE root; the project's home
            # (whose providers.yaml may shadow the engine's) rides the run
            # context, which the registry loader reads.
            from mini_ork.context import run_context_scope

            with run_context_scope({"MINI_ORK_HOME": str(home)}):
                rows = orchestrator_lanes()
        except Exception:  # noqa: BLE001 — picker must never crash new_session
            return [SessionConfigSelectOption(value="opus", name="opus")]
        out: list[SessionConfigSelectOption] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            lane_id = str(row.get("id") or "").strip()
            if not lane_id:
                continue
            lane_name = str(row.get("name") or lane_id)
            out.append(SessionConfigSelectOption(value=lane_id, name=lane_name))
        if not out:
            out = [SessionConfigSelectOption(value="opus", name="opus")]
        return out

    def _build_recipe_config_option(
        self, session_id: str
    ) -> SessionConfigOptionSelect:
        cfg = self._thread_config.get(session_id) or {}
        recipes = self._list_recipes()
        current = str(cfg.get("recipe") or "")
        if current and current not in recipes:
            current = recipes[0] if recipes else ""
        if not current and recipes:
            current = recipes[0]
        return SessionConfigOptionSelect(
            type="select",
            id="recipe",
            name="Recipe",
            description="Recipe used by direct-mode prompts.",
            current_value=current,
            options=[
                SessionConfigSelectOption(value=name, name=name)
                for name in recipes
            ],
        )

    def _list_recipes(self) -> list[str]:
        """Recipe ids from ``web.recipes.list_recipes``, sorted.

        Defensive against a missing recipes dir or yaml loader failure — the
        picker falls back to the agent's default recipe so it always has at
        least one option.
        """
        try:
            from mini_ork.web.recipes import list_recipes
        except Exception:  # noqa: BLE001 — picker must never crash new_session
            return [self._recipe]
        try:
            names = list_recipes()
        except Exception:  # noqa: BLE001 — picker must never crash new_session
            return [self._recipe]
        out = [str(n) for n in names if isinstance(n, str)]
        if not out:
            out = [self._recipe]
        return sorted(out)

    def _build_session_modes(self, session_id: str) -> SessionModeState:
        cfg = self._thread_config.get(session_id) or {}
        return SessionModeState(
            current_mode_id=str(cfg.get("mode") or _MODE_ORCHESTRATE),
            available_modes=[
                SessionMode(
                    id=_MODE_ORCHESTRATE,
                    name="Orchestrate",
                    description="Talk to the mini-ork orchestrator",
                ),
                SessionMode(
                    id=_MODE_DIRECT,
                    name="Direct run",
                    description="Each prompt is a run kickoff",
                ),
            ],
        )

    def _current_config_options(self, session_id: str) -> list[SessionConfigOptionSelect]:
        cfg = self._thread_config.get(session_id) or {}
        home = self._home_for(session_id)
        # Refresh the recipe / model picker against current ``_list_*`` so a
        # recipe dir change between sessions is reflected on every config
        # round-trip.
        options = self._build_config_options(session_id, home)
        # Pin current_value to the just-stored field in case the helper fell
        # back to a different value when the stored one was invalid.
        for opt in options:
            opt.current_value = str(cfg.get(opt.id) or opt.current_value or "")
        return options

    # ── config option protocol (Z9c-1) ────────────────────────────────────────

    async def set_config_option(
        self,
        config_id: str,
        session_id: str,
        value: str,
        **kwargs: Any,
    ) -> SetSessionConfigOptionResponse:
        """Validate + store + emit + return the full picker list.

        Invalid ``config_id`` or ``value`` raises ``RequestError.invalid_params``
        (acp SDK convention). The same call emits a ``ConfigOptionUpdate`` so
        clients pick up the change live, then returns the full refreshed list
        wrapped in ``SetSessionConfigOptionResponse``.
        """
        del kwargs
        if session_id not in self._thread_sessions:
            raise RequestError.invalid_params(
                {"message": f"set_config_option on a non-thread session: {session_id!r}"}
            )
        cfg = self._thread_config.setdefault(session_id, {})
        if config_id not in _THREAD_CONFIG_KEYS:
            raise RequestError.invalid_params(
                {"message": f"unknown config option: {config_id!r}"}
            )
        # Validate against the picker source. Unknown modes / lanes / recipes
        # are rejected — the agent's picker is authoritative.
        if config_id == "mode" and value not in (_MODE_ORCHESTRATE, _MODE_DIRECT):
            raise RequestError.invalid_params({"message": f"unknown mode: {value!r}"})
        if config_id == "model":
            lanes = self._list_orchestrator_lanes(self._home_for(session_id))
            valid_ids = {opt.value for opt in lanes}
            if value not in valid_ids:
                raise RequestError.invalid_params(
                    {"message": f"unknown model lane: {value!r}"}
                )
        if config_id == "recipe":
            recipes = self._list_recipes()
            if value not in recipes:
                raise RequestError.invalid_params(
                    {"message": f"unknown recipe: {value!r}"}
                )
        cfg[config_id] = value
        options = self._current_config_options(session_id)
        options_cast = cast(Any, options)
        await self._emit(
            session_id,
            ConfigOptionUpdate(
                session_update="config_option_update", config_options=options_cast
            ),
        )
        return SetSessionConfigOptionResponse(config_options=options_cast)

    async def set_session_mode(
        self, mode_id: str, session_id: str, **kwargs: Any
    ) -> SetSessionModeResponse:
        """Legacy ``session/set_mode`` shim → routes to ``set_config_option``.

        The kickoff keeps ``modes`` in sync with the ``mode`` config option so
        older clients that call ``set_session_mode`` still work; new clients
        go through ``set_config_option`` directly.
        """
        del kwargs
        await self.set_config_option("mode", session_id, mode_id)
        return SetSessionModeResponse()

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
        # Thread (orchestrator) session: route to orchestrate/direct path.
        if session_id in self._thread_sessions:
            return await self._prompt_thread(session_id, prompt)
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

    # ── thread-session prompt paths (Z9c-1) ───────────────────────────────────

    async def _prompt_thread(
        self, session_id: str, prompt: list[Any]
    ) -> PromptResponse:
        """Route a thread-session prompt to orchestrate or direct mode.

        The ``/run <task>`` slash command short-circuits to direct mode for
        that one prompt regardless of the stored mode. Otherwise the stored
        ``mode`` config selects the path. The orchestrator turn is awaited
        inline so cancel can interrupt it; the direct-mode path reuses the
        existing ``_await_terminal`` loop against the fresh run id.
        """
        text = _extract_prompt_text(prompt)
        slash_text = self._strip_slash_run(text)
        if slash_text is not None:
            return await self._prompt_thread_direct(session_id, slash_text)
        cfg = self._thread_config.get(session_id) or {}
        mode = str(cfg.get("mode") or _MODE_ORCHESTRATE)
        if mode == _MODE_DIRECT:
            return await self._prompt_thread_direct(session_id, text)
        return await self._prompt_thread_orchestrate(session_id, text)

    @staticmethod
    def _strip_slash_run(text: str) -> str | None:
        """Return the kickoff text after ``/run ``, or ``None`` if not present."""
        if not text:
            return None
        if not text.startswith(_SLASH_RUN_PREFIX):
            return None
        return text[len(_SLASH_RUN_PREFIX):].strip()

    async def _prompt_thread_direct(
        self, session_id: str, text: str
    ) -> PromptResponse:
        """Direct-mode path: launch a fresh run + await its terminal status.

        Mirrors the run-session path (``launcher + _await_terminal``) but
        with the thread session's stored recipe. The new run id is a fresh
        mint; the thread session id is NOT a run id and is never passed to
        ``_reader`` / ``_stopper`` / ``_killer``.
        """
        if not text:
            return PromptResponse(stop_reason="refusal")
        cfg = self._thread_config.get(session_id) or {}
        recipe = str(cfg.get("recipe") or self._recipe)
        new_run_id = mint_run_id()
        self._sessions[new_run_id] = self._sessions.get(session_id, os.getcwd())
        self._recipes[new_run_id] = recipe
        self._launch_count += 1
        launcher = self._launcher or self._launch
        result = launcher(new_run_id, text)
        self._launches[new_run_id] = result if isinstance(result, dict) else {}
        if isinstance(result, dict) and result.get("ok") is False:
            return PromptResponse(stop_reason="refusal")
        return PromptResponse(stop_reason=await self._await_terminal(new_run_id))

    async def _prompt_thread_orchestrate(
        self, session_id: str, text: str
    ) -> PromptResponse:
        """Orchestrate-mode path: one orchestrator turn against the stored lane.

        The ``on_event`` callback maps each stream-json block through
        ``orchestration.map_event`` and emits the resulting updates on the
        agent's connection. The returned ``claude_session_id`` is persisted
        for the next turn's ``--resume``. A non-zero return code yields an
        agent message with the error tail and ``stop_reason="end_turn"``.
        Cancel cancels the orchestrator task; the harness kills its process.
        """
        if not text:
            return PromptResponse(stop_reason="refusal")
        cfg = self._thread_config.get(session_id) or {}
        lane = str(cfg.get("model") or "opus")
        resume = str(cfg.get("claude_session_id") or "") or None
        cwd = self._sessions.get(session_id) or os.getcwd()
        home = self._home_for(session_id)
        turn = self._orchestrator_turn or self._default_orchestrator_turn
        session_ref = {"turn_result": None}

        # tool_use id -> recipe for this turn's start_run calls. Only results
        # answering one of these start a child run: run_status / wait_for_run /
        # run_detail results also carry a run_id but must not.
        start_run_calls: dict[str, str] = {}

        async def on_event(event: dict[str, Any]) -> None:
            for update in _orchestration.map_event(event):
                await self._emit(session_id, update)
            if event.get("type") == "assistant":
                for block in (event.get("message") or {}).get("content") or []:
                    if (isinstance(block, dict) and block.get("type") == "tool_use"
                            and str(block.get("name") or "").split("__")[-1]
                            == _orchestration.CHILD_RUN_TOOL):
                        inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                        start_run_calls[str(block.get("id") or "")] = str(inp.get("recipe") or "")
            child = self._extract_child_run_from_event(event, start_run_calls)
            if child:
                await self._start_child_follow(session_id, child[0], recipe=child[1])

        async def _run() -> Any:
            return await turn(
                lane=lane,
                prompt=text,
                cwd=Path(cwd),
                home=home,
                resume=resume,
                on_event=on_event,
            )

        task = asyncio.create_task(_run())
        self._orchestrator_tasks[session_id] = task
        try:
            result = await task
        except asyncio.CancelledError:
            return PromptResponse(stop_reason="cancelled")
        finally:
            self._orchestrator_tasks.pop(session_id, None)
        session_ref["turn_result"] = result
        if result is None:
            return PromptResponse(stop_reason="cancelled")
        rc = getattr(result, "rc", 0)
        new_session_id = getattr(result, "session_id", None)
        if isinstance(new_session_id, str) and new_session_id:
            cfg["claude_session_id"] = new_session_id
        if rc != 0:
            error_tail = str(getattr(result, "error", "") or "")
            text_tail = str(getattr(result, "text", "") or "")
            await self._emit(
                session_id,
                self._build_refusal_message(
                    f"Orchestrator turn failed (rc={rc}): {error_tail or text_tail}"
                ),
            )
            return PromptResponse(stop_reason="end_turn")
        return PromptResponse(stop_reason="end_turn")

    def _extract_child_run_from_event(
        self, event: dict[str, Any], start_run_calls: dict[str, str]
    ) -> tuple[str, str] | None:
        """Pull ``(tool_name, tool_result_text)`` out of a user tool_result.

        Returns the extracted ``run_id`` when the tool is the canonical
        ``start_run`` MCP tool and its first text block parses as JSON with a
        safe-token ``run_id`` field. Otherwise ``None`` — the orchestrator
        turn continues unchanged.
        """
        if not isinstance(event, dict) or event.get("type") != "user":
            return None
        message = event.get("message")
        if not isinstance(message, dict):
            return None
        blocks = message.get("content")
        if not isinstance(blocks, list):
            return None
        # Walk in reverse to land on the most recent tool_result; the
        # orchestrator's stream-json events always pair one user envelope
        # with one tool_result, so order is not load-bearing.
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_use_id = str(block.get("tool_use_id") or "")
            # The mapper stripped the MCP prefix in ``_tool_title``; the
            # tool_use name for ``mcp__mini-ork__start_run`` is unknown here,
            # so we accept any tool_use_id and let the orchestrator seam
            # route on the raw name. We rely on the implementation of
            # ``orchestration.extract_child_run`` to check the tool name
            # via the title we projected on the ToolCallStart — but here
            # we don't have that title. Fall back to checking the
            # well-known tool name string: only "start_run" (after the MCP
            # prefix) is treated as a child-run trigger.
            inner = block.get("content")
            if not isinstance(inner, list):
                continue
            text_parts: list[str] = []
            for c in inner:
                if isinstance(c, dict):
                    t = c.get("text")
                    if isinstance(t, str) and t:
                        text_parts.append(t)
                        break
            text = "".join(text_parts)
            if not text:
                continue
            if tool_use_id not in start_run_calls:
                continue
            run_id = _orchestration.extract_child_run(_orchestration.CHILD_RUN_TOOL, text)
            if run_id:
                return run_id, start_run_calls[tool_use_id]
        return None

    async def _start_child_follow(
        self, parent_session_id: str, child_run_id: str, recipe: str = ""
    ) -> None:
        """Spawn a child-run follower under prefixed tool ids.

        Emits a ``ToolCallStart`` parent marker titled ``run <run_id> (<recipe>)``
        and creates an asyncio task that mirrors ``_follow`` against the child
        run. The follower is stored under the parent session id so ``cancel``
        can find it. Multiple concurrent child runs are independent.
        """
        if not _is_safe_token(child_run_id):
            return
        existing = self._followers.get(child_run_id)
        if existing is not None and not existing.done():
            return  # already following
        cfg = self._thread_config.get(parent_session_id) or {}
        recipe = recipe or str(cfg.get("recipe") or self._recipe)
        await self._emit(
            parent_session_id,
            ToolCallStart(
                session_update="tool_call",
                tool_call_id=f"{child_run_id}:parent",
                title=f"run {child_run_id} ({recipe})",
                status="in_progress",
                kind="other",
            ),
        )
        self._followers[child_run_id] = asyncio.create_task(
            self._follow_child(parent_session_id, child_run_id)
        )

    async def _follow_child(
        self, parent_session_id: str, child_run_id: str
    ) -> None:
        """Poll a child run's read model until it reaches a terminal status.

        Mirrors ``_follow`` minus the cancel gate: child runs keep streaming
        after the orchestrator's turn ends until they go terminal or the
        parent session is cancelled. Node lifecycle events project under the
        prefixed tool id ``f"<child_run_id>:<node_id>"`` so they never collide
        with the orchestrator's own ids.
        """
        reader = self._reader or self._read_snapshot
        prefix = f"{child_run_id}:"
        while parent_session_id not in self._cancelled:
            snapshot = reader(child_run_id) or {
                "status": None,
                "events": [],
                "llm_calls": [],
            }
            # Project lifecycle events under the prefixed tool id so they
            # land on the right parent marker.
            emitted = self._emitted.setdefault(child_run_id, set())
            for ev in snapshot.get("events") or []:
                payload = ev.get("payload_json") or {}
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except (json.JSONDecodeError, TypeError):
                        payload = {}
                node_id = str((payload or {}).get("node_id") or "node")
                event_type = str(ev.get("event_type") or "")
                if event_type == "node_start":
                    key = f"{node_id}:start"
                    if key in emitted:
                        continue
                    emitted.add(key)
                    await self._emit(
                        parent_session_id,
                        ToolCallStart(
                            session_update="tool_call",
                            tool_call_id=f"{prefix}{node_id}",
                            title=str((payload or {}).get("node_type") or node_id),
                            status="in_progress",
                        ),
                    )
                elif event_type == "node_end":
                    key = f"{node_id}:end"
                    if key in emitted:
                        continue
                    emitted.add(key)
                    await self._emit(
                        parent_session_id,
                        ToolCallProgress(
                            session_update="tool_call_update",
                            tool_call_id=f"{prefix}{node_id}",
                            status="completed",
                        ),
                    )
            if snapshot.get("status") in TERMINAL_STATUSES:
                break
            await asyncio.sleep(self._poll_interval)
        self._followers.pop(child_run_id, None)

    async def _default_orchestrator_turn(
        self,
        *,
        lane: str,
        prompt: str,
        cwd: Path,
        home: Path,
        resume: str | None,
        on_event: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> Any:
        """Default ``orchestrator_turn`` — delegates to the orchestrator harness.

        Deferred so the orchestrator package's heavy imports (dispatch layer,
        provider registry) only land when an orchestrator turn actually runs,
        not at agent construction time. The CLI process spawns a real
        ``claude`` subprocess here; tests inject a fake.
        """
        from mini_ork.acp_orchestrator.harness import run_turn

        return await run_turn(
            lane=lane,
            prompt=prompt,
            cwd=cwd,
            home=home,
            resume=resume,
            on_event=on_event,
        )

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        del kwargs
        self._cancelled.add(session_id)
        follower = self._followers.get(session_id)
        if follower is not None and not follower.done():
            follower.cancel()
        if session_id in self._thread_sessions:
            # Thread session: cancel the orchestrator turn (the harness
            # terminates the claude subprocess) and stop any child-run
            # followers spawned by start_run tool_results. The child runs
            # themselves keep running (kickoff: child runs are stopped via
            # ``/stop`` later, not on orchestrator cancel).
            task = self._orchestrator_tasks.get(session_id)
            if task is not None and not task.done():
                task.cancel()
            for child_run_id, child_task in list(self._followers.items()):
                if child_task is not None and not child_task.done():
                    child_task.cancel()
                self._followers.pop(child_run_id, None)
            return
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
