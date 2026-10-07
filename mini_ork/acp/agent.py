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
import base64
import binascii
import datetime as _dt
import json
import re
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, cast

from acp import RequestError
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    AgentPlanUpdate,
    AgentThoughtChunk,
    AuthenticateResponse,
    AvailableCommandsUpdate,
    ConfigOptionUpdate,
    ContentToolCallContent,
    Cost,
    FileEditToolCallContent,
    ImageContentBlock,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PermissionOption,
    PlanEntry,
    PromptCapabilities,
    PromptResponse,
    ResourceContentBlock,
    ToolCallUpdate,
    SessionCapabilities,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SessionInfo,
    SessionInfoUpdate,
    SessionListCapabilities,
    SessionMode,
    SessionModeState,
    SessionNotification,
    SetSessionConfigOptionResponse,
    SetSessionModeResponse,
    StopReason,
    TerminalAuthMethod,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    UsageUpdate,
    UserMessageChunk,
)

from mini_ork.acp import commands as _commands
from mini_ork.acp import diffs as _diffs
from mini_ork.acp import history
from mini_ork.acp import orchestration as _orchestration
from mini_ork.acp import plan as _plan
from mini_ork.acp import race as _race
from mini_ork.acp import task_state as _task_state
from mini_ork.acp.task_state import TaskState as _TaskState
from mini_ork.acp.live import LiveTail, normalize
from mini_ork.acp.threads import ThreadStore, title_from_text
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
# Opt-out for the new_session thread-branch setup gate. Read at call time
# (not at import time) so tests can flip the env var via an autouse fixture
# and existing module tests do not hit the real ``claude auth status --json``
# call. Default: gate enabled. Set ``MO_ACP_SKIP_SETUP_CHECK=1`` to disable.
# Per-cwd cache of the last orchestrator-check pass timestamp (epoch seconds).
# A passing result is cached for ``_SETUP_PASS_TTL_S`` so we don't pay the
# ``claude auth status`` call on every thread start.
_SETUP_PASS_TTL_S = 600
_SETUP_PASS_CACHE: dict[str, float] = {}

# ACP terminal-auth method id and args (kickoff §agent.py). The Registry
# greps for this literal id; do not rename without touching the registry.
_SETUP_METHOD_ID = "mini-ork-setup"
_SETUP_METHOD_ARGS = ["acp", "--setup"]


def _setup_readiness_for_thread(cwd: str):
    """Return the readiness checks for ``Path(cwd)`` (thread-branch helper).

    Module-level so ``asyncio.to_thread`` can call it without binding to the
    instance. Importing the setup module here is fine — it has no dependency
    on this file.
    """
    from mini_ork.acp.setup import readiness

    return readiness(Path(cwd))


def _setup_readiness(cwd: Path):
    """Return readiness for ``Path`` (authenticate helper, unit-test seam)."""
    from mini_ork.acp.setup import readiness

    return readiness(cwd)

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


def task_name_for_kickoff(kickoff_text: str) -> str:
    """First non-empty kickoff line with leading ``#`` stripped.

    Direct-mode prompts and orchestrator kickoffs are markdown — the title
    is the first heading line (or any non-empty line when no heading). The
    title is then slugified in :func:`mini_ork.workspaces.task_name` to
    become the worktree directory name (Z-W1). Returns ``""`` when the
    text is empty or has no non-empty line.
    """
    for raw in (kickoff_text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            line = line.lstrip("#").strip()
        return line
    return ""


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
_THREAD_CONFIG_KEYS = ("mode", "model", "recipe", "workspace")

# Mode ids the kickoff mandates. Anything else is rejected by
# ``set_config_option`` as ``invalid_params``.
_MODE_ORCHESTRATE = "orchestrate"
_MODE_DIRECT = "direct"

# Workspace mode ids (Zed S4). ``worktree`` isolates the run on its own
# git branch under ``<home>/worktrees/<run_id>``; ``in-place`` runs
# against the user's checkout. Default honours ``MO_WORKSPACE_MODE`` so
# operators can opt out per-process.
_WORKSPACE_WORKTREE = "worktree"
_WORKSPACE_IN_PLACE = "in-place"
_WORKSPACE_VALUES = (_WORKSPACE_WORKTREE, _WORKSPACE_IN_PLACE)

# Slash command that triggers direct-mode behaviour for a single prompt
# regardless of the session's stored mode. The text after the prefix is
# the kickoff body passed to the launcher.
_SLASH_RUN_PREFIX = "/run "

# ``/race <task>`` mirrors ``/run`` but fans out across lanes. The text
# after the prefix is the same kickoff body; the race turn launches one
# run per lane, each in its own git worktree, and decides which one to
# keep via a follow-up permission prompt.
_SLASH_RACE_PREFIX = "/race "


def _extract_prompt_text(prompt: list[Any]) -> str:
    """Flatten the text content blocks of an ACP prompt into one string."""
    chunks: list[str] = []
    for block in prompt or []:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text.strip():
            chunks.append(text)
    return "\n".join(chunks)


# Extension for ``image/png`` → ``.png``. The ACP client sends ``mimeType``
# (camelCase JSON) which Pydantic deserialises to ``mime_type`` on the model.
_ATTACHMENT_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def _attachment_extension(mime_type: str) -> str:
    """Map an ACP image mime type to a filesystem extension; ``.bin`` fallback."""
    return _ATTACHMENT_EXT_BY_MIME.get(str(mime_type or "").lower(), ".bin")


def _write_attached_image(
    home: Path, session_id: str, n: int, block: ImageContentBlock
) -> Path:
    """Decode an image block to ``<home>/attachments/<session>/<n>.<ext>``.

    The directory is created lazily; an existing ``<n>.<ext>`` is replaced
    (re-sending the same image is idempotent). Decode failures raise — the
    prompt caller surfaces them. The returned path is what the orchestrator
    is told to ``Read``.
    """
    target_dir = home / "attachments" / session_id
    target_dir.mkdir(parents=True, exist_ok=True)
    ext = _attachment_extension(getattr(block, "mime_type", "") or "")
    target = target_dir / f"{n}{ext}"
    raw = base64.b64decode(str(getattr(block, "data", "") or ""), validate=True)
    target.write_bytes(raw)
    return target


def _build_prompt_payload(
    prompt: list[Any], session_id: str, home: Path
) -> str:
    """Render an ACP prompt into the string the orchestrator / launcher sees.

    Text blocks join with newlines (same shape as ``_extract_prompt_text``).
    Image blocks (base64 ``data`` + ``mime_type``) decode to
    ``<home>/attachments/<session>/<n>.<ext>`` and the prompt gains an
    ``Attached image: <abs path>`` line so the orchestrator can ``Read`` it.
    Embedded text resources inline as ``--- <uri> ---\\n<text>`` (blob
    resources append a placeholder line — Claude can read images directly).
    Resource links append ``Attached: <uri>``. Text-only prompts are
    unchanged.

    Slash-command detection still calls ``_extract_prompt_text`` so this
    helper is for the orchestrate / direct paths only.
    """
    chunks: list[str] = []
    image_counter = 0
    for block in prompt or []:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text.strip():
            chunks.append(text)
            continue
        # ImageContentBlock carries base64 ``data`` + ``mime_type``.
        if isinstance(block, ImageContentBlock):
            try:
                path = _write_attached_image(
                    home, session_id, image_counter, block
                )
            except (binascii.Error, ValueError, OSError) as exc:
                # Surface a clear hint in the prompt so the orchestrator
                # can tell the user; the run is still launched.
                chunks.append(f"[attached image decode failed: {exc}]")
                image_counter += 1
                continue
            chunks.append(f"Attached image: {path}")
            image_counter += 1
            continue
        # ResourceContentBlock (the wire-level resource link) advertises a
        # uri the user wants to reference.
        if isinstance(block, ResourceContentBlock):
            uri = getattr(block, "uri", "") or ""
            if uri:
                chunks.append(f"Attached: {uri}")
            continue
        # EmbeddedResource carries an inline payload (text or blob).
        resource = getattr(block, "resource", None)
        if resource is not None:
            uri = getattr(resource, "uri", "") or ""
            mime = getattr(resource, "mime_type", "") or ""
            text_body = getattr(resource, "text", None)
            if isinstance(text_body, str) and text_body:
                header = f"--- {uri} ---" if uri else f"--- {mime or 'attachment'} ---"
                chunks.append(f"{header}\n{text_body}")
            elif uri:
                chunks.append(f"Attached: {uri} ({mime or 'binary'})")
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


def _draft_result(event: dict[str, Any], draft_calls: set[str]) -> dict[str, Any] | None:
    """A successful ``draft_recipe`` tool result in ``event`` (a stream-json user
    message answering one of ``draft_calls``), as ``{draft_id, tool_call_id,
    exists, grade, warnings}``; None otherwise."""
    if event.get("type") != "user" or not draft_calls:
        return None
    for block in (event.get("message") or {}).get("content") or []:
        if not (isinstance(block, dict) and block.get("type") == "tool_result"):
            continue
        tool_call_id = str(block.get("tool_use_id") or "")
        if tool_call_id not in draft_calls:
            continue
        raw = block.get("content")
        if isinstance(raw, list):
            raw = "".join(c.get("text", "") for c in raw if isinstance(c, dict))
        try:
            data = json.loads(raw) if isinstance(raw, str) else None
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and data.get("ok") and data.get("draft_id"):
            return {"draft_id": str(data["draft_id"]), "tool_call_id": tool_call_id,
                    "exists": bool(data.get("exists")), "grade": data.get("grade") or {},
                    "warnings": data.get("warnings") or []}
    return None


def _proposal_result(event: dict[str, Any], propose_calls: set[str]) -> dict[str, Any] | None:
    """A successful ``propose_automation`` tool result in ``event`` (a stream-json
    user message answering one of ``propose_calls``), as ``{proposal_id,
    tool_call_id, exists, name, recipe, schedule, when, kickoff, next_fires,
    workspace}``; ``None`` otherwise.

    Mirrors :func:`_draft_result` so a hermetic test can swap the live MCP
    call for a fake orchestrator turn that emits the same JSON shape (see
    the kickoff's "real ``automations.propose`` in a temp home" guidance).
    The shape mirrors :func:`mini_ork.automations.propose`'s success
    payload (``ok, proposal, exists, when, next_fires``). The proposal
    payload itself carries ``name, recipe, kickoff, schedule, workspace``.
    """
    if event.get("type") != "user" or not propose_calls:
        return None
    for block in (event.get("message") or {}).get("content") or []:
        if not (isinstance(block, dict) and block.get("type") == "tool_result"):
            continue
        tool_call_id = str(block.get("tool_use_id") or "")
        if tool_call_id not in propose_calls:
            continue
        raw = block.get("content")
        if isinstance(raw, list):
            raw = "".join(c.get("text", "") for c in raw if isinstance(c, dict))
        try:
            data = json.loads(raw) if isinstance(raw, str) else None
        except json.JSONDecodeError:
            data = None
        if not (isinstance(data, dict) and data.get("ok")):
            return None
        proposal = data.get("proposal") if isinstance(data.get("proposal"), dict) else {}
        proposal_id = str(proposal.get("id") or "")
        if not proposal_id:
            return None
        return {
            "proposal_id": proposal_id,
            "tool_call_id": tool_call_id,
            "exists": bool(data.get("exists")),
            "name": str(proposal.get("name") or proposal_id),
            "recipe": str(proposal.get("recipe") or ""),
            "schedule": str(proposal.get("schedule") or ""),
            "when": str(data.get("when") or ""),
            "kickoff": str(proposal.get("kickoff") or ""),
            "next_fires": list(data.get("next_fires") or []),
            "workspace": str(proposal.get("workspace") or "worktree"),
        }
    return None


def _kickoff_result(event: dict[str, Any], kickoff_calls: set[str]) -> dict[str, Any] | None:
    """A successful ``draft_kickoff`` tool result in ``event`` (a stream-json
    user message answering one of ``kickoff_calls``), as ``{draft_id,
    tool_call_id, recipe, path, findings, ok}``; ``None`` otherwise.

    Mirrors :func:`_draft_result` and :func:`_proposal_result`. The
    payload shape is ``{"ok", "draft_id", "recipe", "path", "findings"}``
    — the same JSON :func:`mini_ork.mcp_context.server._draft_kickoff`
    returns. An ``ok: false`` envelope (unknown recipe) is still surfaced
    so the orchestrator turn can iterate; the agent itself does not
    offer buttons in that case (kickoff §Agent: "``ok: false`` asks
    nothing").
    """
    if event.get("type") != "user" or not kickoff_calls:
        return None
    for block in (event.get("message") or {}).get("content") or []:
        if not (isinstance(block, dict) and block.get("type") == "tool_result"):
            continue
        tool_call_id = str(block.get("tool_use_id") or "")
        if tool_call_id not in kickoff_calls:
            continue
        raw = block.get("content")
        if isinstance(raw, list):
            raw = "".join(c.get("text", "") for c in raw if isinstance(c, dict))
        try:
            data = json.loads(raw) if isinstance(raw, str) else None
        except json.JSONDecodeError:
            data = None
        if not isinstance(data, dict):
            return None
        draft_id = str(data.get("draft_id") or "")
        if data.get("ok") and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", draft_id):
            return None  # it becomes a file name under the home
        return {
            "ok": bool(data.get("ok")),
            "draft_id": draft_id,
            "tool_call_id": tool_call_id,
            "recipe": str(data.get("recipe") or ""),
            "path": str(data.get("path") or ""),
            "findings": list(data.get("findings") or []),
        }
    return None


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
        default_workspace: str | None = None,
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
        # Workspace mode (Zed S4). ``worktree`` isolates each run on its own
        # git branch; ``in-place`` runs against the user's checkout. The
        # ``MO_WORKSPACE_MODE`` env wins the same way ``MO_ACP_DEFAULT_MODE``
        # does, then the ctor arg, then the literal ``"worktree"`` default.
        # Invalid values fall back to ``worktree`` rather than raise — the
        # picker rejects them on the way out, but the ctor must not crash.
        env_ws = os.environ.get("MO_WORKSPACE_MODE")
        ws = default_workspace or env_ws or _WORKSPACE_WORKTREE
        if ws not in _WORKSPACE_VALUES:
            ws = _WORKSPACE_WORKTREE
        self._default_workspace = ws
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
        # run id → (thread session id, tool-call id prefix). A run followed
        # inside a thread (orchestrator child run, direct mode, ``/run``) goes
        # through the normal run projection; ``_emit`` re-addresses it.
        self._routes: dict[str, tuple[str, str]] = {}
        # run id → last status the projection saw (closes the run's marker).
        self._run_status: dict[str, str] = {}
        # thread session id → {"orchestrator" | run id: cost USD}; the thread
        # reports one cumulative cost instead of the latest run's.
        self._thread_costs: dict[str, dict[str, float]] = {}
        # thread session id → ordered list of run ids this thread has followed
        # (Z5). Append sites: ``_start_child_follow`` and ``_prompt_thread_direct``.
        # Rebuild site: ``_load_thread_session`` scans the replayed
        # ``ToolCallStart`` updates whose ``tool_call_id`` ends with ``:parent``.
        # Commands that act on a run resolve the "current run" to ``runs[-1]``
        # unless the user typed an explicit run id.
        self._thread_runs: dict[str, list[str]] = {}
        # thread session id → last cost total sent (each poll re-reports usage).
        self._usage_sent: dict[str, float] = {}
        # thread session ids that have already received their first user
        # prompt (the ``SessionInfoUpdate`` title-push lives on this flag —
        # a loaded thread arrives with the flag already set so replay does
        # not re-emit the title update).
        self._first_prompt_sent: set[str] = set()
        # thread session id → rewrite intent text injected on the next
        # ``_prompt_thread`` call (S3b-2). ``/recipe new`` and ``/recipe edit``
        # stash the orchestrator-facing text via ``_commands._RewriteToOrchestrate``;
        # ``_dispatch_slash`` stores the intent here, then returns None so the
        # caller falls through to ``_prompt_thread`` which picks it up.
        self._thread_rewrites: dict[str, str] = {}
        # session ids currently being loaded from a persisted thread. A
        # replayed update must NOT be re-recorded (kickoff §"load_session"):
        # the simplest guard is a per-load set checked at the top of
        # ``_emit``.
        self._replaying: set[str] = set()
        # thread session id → the direct-mode run its current prompt awaits.
        self._direct_runs: dict[str, str] = {}
        # (session, node) pairs whose agent already streamed text — a final
        # ``result`` event is shown only for nodes that streamed nothing.
        self._text_seen: set[tuple[str, str]] = set()
        # (session, node) pairs whose diffs were already emitted for an
        # implementer ``node_end``. Z4 dedup: a live poll cycle that re-runs
        # the same snapshot must not re-emit the diff update. Mirrors the
        # ``_emitted`` lifecycle set but is keyed independently — a load that
        # resets ``_emitted`` must also reset this set so the first emit of
        # the load fires.
        self._diff_emitted: set[tuple[str, str]] = set()
        # destination session id → last canonicalised tuple of plan entries
        # sent (Z6 dedup). A projection pass that recomputes the same entries
        # is a no-op emit. Keyed by the post-``_emit`` destination so a
        # thread with multiple child runs sees one plan; a load that resets
        # ``_emitted`` must also reset the matching entry so replay fires.
        self._plan_emitted: dict[str, tuple[tuple[str, str, str], ...]] = {}
        # session id → node_id → LiveTail (Z3 live projection). Constructed
        # lazily on first node_start; held until session end.
        self._tails: dict[str, dict[str, LiveTail]] = {}
        # destination session id → last task_state title sent (Zed S1).
        # Keyed on the post-``_emit`` destination so a thread sees one
        # title per child-run state change. Mirrors ``_plan_emitted``'s
        # dedup shape. Reset by ``load_session`` and
        # ``_replay_run_in_thread`` so a 2nd load fires again.
        self._last_title_sent: dict[str, str] = {}
        # destination session id → last ``needs_you`` detail sent. A run
        # may toggle working → needs_you → working → needs_you across
        # /resume calls, so dedup is per-state-epoch (not forever): the
        # entry is cleared whenever the state flips OUT of needs_you.
        self._last_needs_you_sent: dict[str, str] = {}
        # thread session id → its base (first-prompt) title (Zed S1).
        # The task-state title push prefixes the mark; the base is the
        # kickoff-derived or first-prompt text. Set when the first
        # ``SessionInfoUpdate`` lands; restored on thread load from the
        # last ``title`` record (when present) or the first user prompt.
        self._thread_titles: dict[str, str] = {}
        # run session id → its base title (from the kickoff). Used by
        # ``list_sessions`` to render the run row's prefixed title.
        self._run_base_titles: dict[str, str] = {}
        # run id → True once we have offered the review buttons / emitted
        # the one-time "ready to review" message. A child run that ends
        # after the orchestrator's own turn is the most common case; the
        # flag prevents the message from re-firing on every poll. A
        # successful merge / discard clears the entry (no second offer).
        self._ready_to_review_emitted: set[str] = set()
        # Per-run env overlay merged into ``_launch``'s ``extra_env``.
        # Zed S7a: the race turn seeds each run's entry so
        # ``MO_ROUTING_POLICY=workflow_default`` survives the spawn.
        # Cleared on merge / discard / completion so it doesn't leak.
        self._run_env: dict[str, dict[str, str]] = {}
        # Run ids whose review buttons the agent should NOT offer (Zed
        # S7a race turns). The race's parent permission owns the keep /
        # later / discard decision; per-run S5 buttons would re-ask the
        # same question on a separate marker.
        self._skip_review: set[str] = set()
        # Zed S7a: thread id → the run ids of its race in progress, so
        # cancelling the turn stops every contestant.
        self._race_runs: dict[str, list[str]] = {}
        self._launch_count = 0
        # ACP client capabilities captured at ``initialize`` time (S0). The
        # client sends the SDK object in the initialize handshake; we keep
        # the raw value so ``client_supports`` can answer later requests
        # (e.g. an orchestrator turn needs to know whether form-mode
        # elicitation is on). None until initialize runs.
        self._client_capabilities: Any | None = None

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
        # S0 (zed-integration): keep the capabilities object the client sent
        # in the initialize handshake so ``client_supports`` can answer
        # later. Write ONE stderr line summarising what the editor
        # advertised — later slices key off this so a form-mode elicitation
        # fallback can detect "the editor doesn't support it" without
        # replaying the handshake. ``sys.stdout`` is the ACP wire, so
        # diagnostics must go to ``sys.stderr``.
        del protocol_version, client_info, kwargs
        self._client_capabilities = client_capabilities
        print(
            f"[mini-ork acp] client capabilities: {self._capabilities_summary()}",
            file=sys.stderr,
            flush=True,
        )
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_info=Implementation(name="mini-ork-acp", version="0.9.0"),
            agent_capabilities=AgentCapabilities(
                load_session=True,
                prompt_capabilities=PromptCapabilities(
                    image=True,
                    embedded_context=True,
                ),
                session_capabilities=SessionCapabilities(list=SessionListCapabilities()),
            ),
            auth_methods=[
                TerminalAuthMethod(
                    type="terminal",
                    id=_SETUP_METHOD_ID,
                    name="Set up mini-ork",
                    description=(
                        "Check the project, the orchestrator's Claude login and "
                        "the worker lanes' keys, and fix what is missing."
                    ),
                    args=_SETUP_METHOD_ARGS,
                    env={},
                ),
            ],
        )

    def _capabilities_summary(self) -> str:
        """Render the right-hand side of the capabilities stderr summary.

        Introspects ``acp.schema.ClientCapabilities``'s Python attribute
        names (``fs.read_text_file``, ``fs.write_text_file``,
        ``terminal``, ``elicitation.{form,url}``) so a future schema
        field is auto-detected. Any missing / None / malformed field
        degrades to ``False`` (booleans) or omitted from the elicitation
        list — the stderr line always renders, never raises.
        """
        caps = self._client_capabilities
        fs_read = False
        fs_write = False
        terminal = False
        elicitation: list[str] = []
        if caps is not None:
            try:
                fs = getattr(caps, "fs", None)
                if fs is not None:
                    fs_read = bool(getattr(fs, "read_text_file", False))
                    fs_write = bool(getattr(fs, "write_text_file", False))
            except Exception:
                pass
            try:
                terminal = bool(getattr(caps, "terminal", False))
            except Exception:
                terminal = False
            try:
                elic = getattr(caps, "elicitation", None)
                if elic is not None:
                    for mode in ("form", "url"):
                        if getattr(elic, mode, None) is not None:
                            elicitation.append(mode)
            except Exception:
                elicitation = []
        summary = ",".join(elicitation) if elicitation else "none"
        return f"fs.read={fs_read} fs.write={fs_write} terminal={terminal} elicitation={summary}"

    def client_supports(self, feature: str) -> bool:
        """Return True when the ACP client advertised ``feature``.

        Recognised feature keys: ``"fs.read"``, ``"fs.write"``,
        ``"terminal"``, ``"elicitation.form"``. Anything else returns
        False. A client that never sent ``client_capabilities`` answers
        False for every feature.
        """
        caps = self._client_capabilities
        if caps is None:
            return False
        if feature == "fs.read":
            return bool(
                getattr(getattr(caps, "fs", None), "read_text_file", False)
            )
        if feature == "fs.write":
            return bool(
                getattr(getattr(caps, "fs", None), "write_text_file", False)
            )
        if feature == "terminal":
            return bool(getattr(caps, "terminal", False))
        if feature == "elicitation.form":
            return (
                getattr(getattr(caps, "elicitation", None), "form", None)
                is not None
            )
        return False

    async def authenticate(self, method_id: str, **kwargs: Any) -> AuthenticateResponse:
        """Resolve the ``auth_methods`` advertised by ``initialize``.

        Unknown ``method_id`` → invalid params. Known method → run readiness in
        a background thread; on success return an empty ``AuthenticateResponse``
        (Zed proceeds to ``session/new``), on failure raise
        ``RequestError.auth_required`` so Zed offers the setup terminal.
        """
        del kwargs
        if method_id != _SETUP_METHOD_ID:
            raise RequestError.invalid_params(
                {"message": f"unknown auth method: {method_id!r}"}
            )
        cwd = Path(self._resolve_home()).parent
        checks = await asyncio.to_thread(_setup_readiness, cwd)
        failing = [c for c in checks if not c.ok]
        if not failing:
            return AuthenticateResponse()
        lines = [f"✗ {c.name} — {c.detail}" for c in failing]
        raise RequestError.auth_required({"message": "\n".join(lines)})

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
            await self._emit_available_commands(run_id)
            return NewSessionResponse(session_id=run_id, field_meta={"run_id": run_id})
        # Thread session (Z9c-1). The session id is NOT a run id; it carries
        # the thread config (mode / model / recipe) plus the persisted claude
        # session id (set on first orchestrator turn). The three pickers are
        # Zed-rendered via ``category="model" | "mode"``.
        # Z10: when the orchestrator's Claude login is absent, raise
        # ``auth_required`` so Zed offers the ``mini-ork acp --setup`` terminal.
        # Project / lanes checks do NOT block a thread — direct-mode runs
        # report their own errors. Cache a passing result per cwd for
        # ``_SETUP_PASS_TTL_S`` so the ``claude auth status`` call is amortised.
        # Opt-out: set ``MO_ACP_SKIP_SETUP_CHECK=1`` (default for unit tests).
        if os.environ.get("MO_ACP_SKIP_SETUP_CHECK") != "1":
            await self._enforce_thread_setup(cwd)
        thread_id = _mint_thread_id()
        self._sessions[thread_id] = cwd
        self._thread_sessions.add(thread_id)
        self._thread_config[thread_id] = self._initial_thread_config(cwd)
        home = self._home_for(thread_id)
        config_options = self._build_config_options(thread_id, home)
        # Z9c-2: persist the thread meta + initial config. The store
        # swallows I/O errors (a read-only home logs to stderr) so the
        # new_session response is unaffected.
        self._record(
            thread_id,
            {"type": "meta", "thread_id": thread_id, "cwd": cwd},
        )
        self._record(
            thread_id,
            {
                "type": "config",
                "mode": self._thread_config[thread_id]["mode"],
                "model": self._thread_config[thread_id]["model"],
                "recipe": self._thread_config[thread_id]["recipe"],
                "workspace": self._thread_config[thread_id].get("workspace", ""),
            },
        )
        await self._emit_available_commands(thread_id)
        return NewSessionResponse(
            session_id=thread_id,
            config_options=cast(Any, config_options),
            field_meta={"kind": "thread"},
        )

    # ── thread-session helpers (Z9c-1) ────────────────────────────────────────

    def _initial_thread_config(self, cwd: str) -> dict[str, str]:
        """Per-session defaults populated at ``new_session`` time.

        ``recipe`` falls back to the agent's default recipe; ``mode`` falls
        back to ``_default_mode``; ``model`` falls back to ``_default_model``
        (or the orchestrator's lane-map default for this session's home).
        ``workspace`` falls back to ``_default_workspace`` (Zed S4).
        """
        home = self._home_for_for_cwd(cwd)
        model = self._default_model or self._default_orchestrator_lane(home)
        return {
            "mode": self._default_mode,
            "model": model,
            "recipe": self._default_recipe,
            "workspace": self._default_workspace,
            "claude_session_id": "",
        }

    def _home_for_for_cwd(self, cwd: str) -> Path:
        """Resolve ``.mini-ork`` for a raw cwd (without a session id).

        When ``<cwd>/.mini-ork`` is missing and ``cwd`` is a git linked
        worktree, fall through to ``<main_checkout>/.mini-ork`` so a
        thread opened inside a Zed linked worktree (which is gitignored
        and has no ``.mini-ork`` of its own) still finds the project's
        home. See kickoff §Home resolution.
        """
        candidate = Path(cwd) / ".mini-ork"
        if candidate.is_dir():
            return candidate
        return self._home_in_linked_worktree(Path(cwd)) or self._resolve_home()

    def _home_in_linked_worktree(self, cwd: Path) -> Path | None:
        """``<main>/.mini-ork`` when ``cwd`` is a linked worktree, else ``None``.

        Defensive: the import is inside the helper so a missing helper
        (test harnesses that omit the workspace package) doesn't break
        resolution — the no-home fallback in ``_home_for`` / ``_home_for_for_cwd``
        still kicks in.
        """
        try:
            from mini_ork import workspaces as _ws
        except Exception:  # noqa: BLE001
            return None
        try:
            if not _ws.is_linked_worktree(cwd):
                return None
            main = _ws.main_checkout(cwd)
            if main is None:
                return None
            home = main / ".mini-ork"
        except Exception:  # noqa: BLE001
            return None
        return home if home.is_dir() else None

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

    async def _enforce_thread_setup(self, cwd: str) -> None:
        """Run ``setup.readiness`` for ``cwd``'s orchestrator check, gated.

        Only the ``orchestrator`` check blocks a thread (direct-mode runs
        report their own errors). A passing result is cached per cwd for
        ``_SETUP_PASS_TTL_S`` so the ``claude auth status`` call is amortised.
        Failure raises ``RequestError.auth_required`` so the editor offers the
        terminal-auth setup. The cache key is the raw cwd string (the
        orchestrator's notion of home — ``<cwd>/.mini-ork`` — is what
        ``setup.readiness`` resolves to anyway).
        """
        now = time.monotonic()
        last = _SETUP_PASS_CACHE.get(cwd)
        if last is not None and (now - last) < _SETUP_PASS_TTL_S:
            return
        checks = await asyncio.to_thread(_setup_readiness_for_thread, cwd)
        for c in checks:
            if c.name == "orchestrator" and not c.ok:
                raise RequestError.auth_required(
                    {
                        "message": (
                            f"✗ orchestrator — {c.detail}\n  fix: {c.fix}"
                            if c.fix
                            else f"✗ orchestrator — {c.detail}"
                        )
                    }
                )
        _SETUP_PASS_CACHE[cwd] = now

    def _build_config_options(
        self, session_id: str, home: Path
    ) -> list[SessionConfigOptionSelect]:
        """Materialise the single Zed-rendered picker.

        The thread is a control plane (kickoff §ide-control-plane): only the
        orchestrator lane picker is offered. ``mode``, ``recipe``, and
        ``workspace`` remain stored in ``_thread_config`` so legacy clients
        that call ``set_config_option("recipe", …)`` still succeed and so
        the orchestrator can use the stored recipe / workspace as its
        default when proposing a new run. They are simply not surfaced.
        """
        return [self._build_model_config_option(session_id, home)]

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
        self, session_id: str, home: Path
    ) -> SessionConfigOptionSelect:
        cfg = self._thread_config.get(session_id) or {}
        entries = self._recipe_entries(home)  # one catalog scan per picker build
        recipes = sorted(e.id for e in entries) or [self._recipe]
        current = str(cfg.get("recipe") or "")
        if current and current not in recipes:
            current = recipes[0]
        if not current:
            current = recipes[0]
        by_id = {e.id: e for e in entries}
        options: list[SessionConfigSelectOption] = []
        for rid in recipes:
            entry = by_id.get(rid)
            if entry is not None and entry.source == "project":
                desc = (
                    "project recipe (overrides the engine's)"
                    if entry.shadows_engine
                    else "project recipe"
                )
            else:
                desc = entry.description if entry is not None else ""
            options.append(
                SessionConfigSelectOption(value=rid, name=rid, description=desc)
            )
        return SessionConfigOptionSelect(
            type="select",
            id="recipe",
            name="Recipe",
            description="Recipe used by direct-mode prompts.",
            current_value=current,
            options=options,
        )

    def _build_workspace_config_option(
        self, session_id: str, home: Path
    ) -> SessionConfigOptionSelect:
        """Zed S4 — workspace mode picker (worktree vs in-place).

        Mirrors ``_build_recipe_config_option``'s "no category" pattern
        (``agent.py:798-831``) so the picker renders as a free pick rather
        than a mode / model group. ``current_value`` falls back to the
        stored config, then ``_default_workspace``.

        Z-W1: when the thread's cwd is a linked worktree, the ``worktree``
        option is renamed ``This worktree (<dir>)`` — the picker surfaces
        "this is the worktree you're already in" so the user knows the run
        will ADOPT it (no new worktree).
        """
        cfg = self._thread_config.get(session_id) or {}
        current = str(cfg.get("workspace") or self._default_workspace)
        if current not in _WORKSPACE_VALUES:
            current = _WORKSPACE_WORKTREE
        cwd = self._sessions.get(session_id) or ""
        wt_label = self._worktree_picker_label(Path(cwd)) if cwd else _WORKSPACE_WORKTREE
        if wt_label is not None and self.client_supports("fs.write"):
            wt_label = None  # runs are delivered as agent edits; they never adopt
        wt_name = "New worktree per task" if wt_label is None else f"This worktree ({wt_label})"
        wt_description = (
            "Run commits onto the worktree's existing branch (no new worktree)."
            if wt_label is not None
            else "Each run edits and commits on its own branch."
        )
        return SessionConfigOptionSelect(
            type="select",
            id="workspace",
            name="Workspace",
            description="How each run in this thread touches the project checkout.",
            current_value=current,
            options=[
                SessionConfigSelectOption(
                    value=_WORKSPACE_WORKTREE,
                    name=wt_name,
                    description=wt_description,
                ),
                SessionConfigSelectOption(
                    value=_WORKSPACE_IN_PLACE,
                    name="In place (this checkout)",
                    description="Runs operate against the user's checkout.",
                ),
            ],
        )

    def _worktree_picker_label(self, cwd: Path) -> str | None:
        """The directory name when ``cwd`` is a linked worktree, else ``None``.

        Defensive against a missing workspaces helper — tests that omit the
        workspaces package see a plain "worktree" picker label.
        """
        try:
            from mini_ork import workspaces as _ws
        except Exception:  # noqa: BLE001
            return None
        try:
            if not _ws.is_linked_worktree(cwd):
                return None
            return cwd.name
        except Exception:  # noqa: BLE001
            return None

    def _recipe_entries(self, home: Path | None) -> list[Any]:
        """The project + engine recipe catalog; [] on any failure (the picker
        must never crash new_session)."""
        try:
            from mini_ork import recipes_catalog

            return [e for e in recipes_catalog.list_recipes(home) if getattr(e, "id", None)]
        except Exception:  # noqa: BLE001
            return []

    def _list_recipes(self, home: Path | None) -> list[str]:
        """Recipe ids from the catalog, sorted; the agent's default recipe when
        the catalog yields nothing, so the picker always has an option."""
        return sorted(e.id for e in self._recipe_entries(home)) or [self._recipe]

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
            recipes = self._list_recipes(self._home_for(session_id))
            if value not in recipes:
                raise RequestError.invalid_params(
                    {"message": f"unknown recipe: {value!r}"}
                )
        if config_id == "workspace" and value not in _WORKSPACE_VALUES:
            raise RequestError.invalid_params(
                {"message": f"unknown workspace: {value!r}"}
            )
        cfg[config_id] = value
        # Z9c-2: persist the full current config snapshot so a later
        # ``load_session`` can rebuild the picker. Only after the value is
        # accepted — a rejected value never reaches here.
        self._record(
            session_id,
            {
                "type": "config",
                "mode": cfg.get("mode", ""),
                "model": cfg.get("model", ""),
                "recipe": cfg.get("recipe", ""),
                "workspace": cfg.get("workspace", ""),
            },
        )
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
        run_dir_root = home / "runs"
        sessions = [
            SessionInfo(
                session_id=row["run_id"],
                cwd=str(home.resolve().parent),
                title=(
                    f"{_task_state.run_mark(row.get('status'), run_dir_root / row['run_id'])} "
                    f"{row.get('title') or ''}"
                ),
                updated_at=row.get("updated_at"),
                field_meta={
                    "status": row.get("status"),
                    "recipe": row.get("recipe"),
                    "cost_usd": row.get("cost_usd"),
                },
            )
            for row in rows
        ]
        # The first page also carries the project's orchestrator threads; later
        # pages are runs only (the cursor is the run offset).
        if offset == 0:
            threads = [
                SessionInfo(
                    session_id=row["thread_id"],
                    cwd=row.get("cwd") or str(home.resolve().parent),
                    title=row.get("title"),
                    updated_at=row.get("updated_at"),
                    field_meta={"kind": "thread"},
                )
                for row in ThreadStore(home).list_threads(limit=100)
            ]
            sessions = self._sort_sessions_by_updated(threads + sessions)
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
        # Z9c-2: thread (orchestrator) sessions replay from
        # ``<home>/acp-threads/<id>.jsonl`` — they are NOT run ids and must
        # not hit ``history.read_snapshot`` (which expects a task_runs row).
        if session_id.startswith("orch-"):
            return await self._load_thread_session(cwd, session_id)
        # Bind the session to its project cwd (the session id IS the run id);
        # reset emitted transitions so replay starts clean.
        self._sessions[session_id] = cwd
        self._emitted.pop(session_id, None)
        # Z4: the live diff hook also tracks "did we already emit diffs for
        # this (run, node)" — reset on load so a 2nd load fires again. Mirrors
        # the ``_emitted.pop`` reset above.
        self._diff_emitted = {
            key for key in self._diff_emitted if key[0] != session_id
        }
        # Z6: the live plan hook dedupes by destination session. For a
        # standalone run the destination IS the run id, so pop it; for a
        # thread-routed run the destination will already have been cleared
        # by the thread's own load (or by ``_replay_run_in_thread``). The
        # standalone-pop keeps a finished run's final plan visible on
        # replay (kickoff §"agent.py").
        self._plan_emitted.pop(session_id, None)
        # Zed S1: the title-state dedup lives on the destination (the run
        # id for a standalone load); clear it so a 2nd load re-emits the
        # mark.
        self._last_title_sent.pop(session_id, None)
        self._last_needs_you_sent.pop(session_id, None)
        home = self._home_for(session_id)
        snapshot = history.read_snapshot(home, session_id)
        if snapshot.get("status") is None:
            raise RequestError.invalid_params({"message": f"no mini-ork run {session_id}"})
        self._loaded.add(session_id)
        kickoff = history.kickoff_text(home, session_id)
        # Zed S1: cache the run's base title (kickoff-derived) so the
        # task-state projection can prefix the mark. A loaded run has
        # no client prompt text of its own — the kickoff IS the title.
        if kickoff:
            self._run_base_titles[session_id] = title_from_text(kickoff)
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
        # Z4: surface the implementer's file edits under the implementer node
        # id resolved from the lifecycle events. Cache replay when present,
        # recompute + cache when absent (and prefix a "may have changed"
        # note). Rolled-back runs get the dedicated message instead.
        await self._emit_diff_update_for_loaded_run(session_id, snapshot)
        if snapshot.get("status") not in TERMINAL_STATUSES:
            self._followers[session_id] = asyncio.create_task(self._follow(session_id))
        await self._emit_available_commands(session_id)
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
        # Z5: slash-command short-circuit. This MUST land before the
        # ``_loaded`` early-return below so commands work in attached
        # sessions too (kickoff: "applies to new and loaded sessions alike").
        # ``/run`` keeps flowing through ``_strip_slash_run`` further down.
        text = _extract_prompt_text(prompt)
        if text and text.startswith("/"):
            cmd_resp = await self._dispatch_slash(session_id, text)
            if cmd_resp is not None:
                return cmd_resp
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
        prompt_text = _extract_prompt_text(prompt)
        # Zed S1: cache the run's base title (first-prompt derived) so
        # the task-state projection can prefix the mark on every cycle.
        self._run_base_titles[session_id] = title_from_text(prompt_text)
        result = launcher(session_id, prompt_text)
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

        Slash-command detection still keys on the text-only prompt so an
        image block cannot masquerade as ``/run``. The orchestrate and
        direct paths receive an *augmented* payload — text plus any
        attached images (decoded to ``<home>/attachments/<session>/<n>.<ext>``
        with an ``Attached image: <path>`` line), embedded text resources
        inlined, and resource links advertised. ``_extract_prompt_text``
        still feeds the slash router.
        """
        # A cancel ends one turn, not the thread.
        self._cancelled.discard(session_id)
        text = _extract_prompt_text(prompt)
        # S3b-2: a ``/recipe new`` or ``/recipe edit`` handler may have
        # stashed a rewrite intent via ``_commands._RewriteToOrchestrate``.
        # Apply it BEFORE ``_record`` so the thread log stores the rewritten
        # user text (what the orchestrator actually sees) instead of the
        # literal ``/recipe new`` slash command.
        rewrite = self._thread_rewrites.pop(session_id, None)
        typed = text
        if rewrite:
            text = rewrite
        # Z9c-2: persist the user prompt and (only on the first prompt of
        # this thread) push a SessionInfoUpdate carrying the title derived
        # from the prompt text. The flag is set BEFORE the routing call so
        # a loaded thread that re-prompted arrives with the flag already on
        # (``_load_thread_session`` seeds it during restore) and never
        # re-emits the title update.
        if not rewrite:  # a rewritten command was already recorded as typed
            self._record(session_id, {"type": "user", "text": text})
        if session_id not in self._first_prompt_sent:
            self._first_prompt_sent.add(session_id)
            title = title_from_text(typed)
            if typed.startswith("/recipe new"):
                title = "New recipe: " + (typed[len("/recipe new"):].strip() or "untitled")
            self._thread_titles[session_id] = title
            await self._emit(
                session_id,
                SessionInfoUpdate(
                    session_update="session_info_update",
                    title=title,
                ),
            )
        slash_text = self._strip_slash_run(text)
        if slash_text is not None:
            return await self._prompt_thread_direct(session_id, slash_text)
        race_text = self._strip_slash_race(text)
        if race_text is not None:
            return await self._prompt_thread_race(session_id, race_text)
        cfg = self._thread_config.get(session_id) or {}
        mode = str(cfg.get("mode") or _MODE_ORCHESTRATE)
        # Direct / orchestrate paths receive the augmented payload — the
        # user may have attached images or resources the text-only string
        # would have dropped.
        home = self._home_for(session_id)
        payload = _build_prompt_payload(prompt, session_id, home)
        if not payload and text:
            # No non-text blocks surfaced anything useful — fall back to the
            # text-only string so a prompt of only failed-decode messages
            # still flows.
            payload = text
        if mode == _MODE_DIRECT:
            return await self._prompt_thread_direct(session_id, payload or text)
        return await self._prompt_thread_orchestrate(session_id, payload or text)

    @staticmethod
    def _strip_slash_run(text: str) -> str | None:
        """Return the kickoff text after ``/run ``, or ``None`` if not present."""
        if not text:
            return None
        if not text.startswith(_SLASH_RUN_PREFIX):
            return None
        return text[len(_SLASH_RUN_PREFIX):].strip()

    @staticmethod
    def _strip_slash_race(text: str) -> str | None:
        """Return the kickoff text after ``/race ``, or ``None`` if not present.

        Mirror of :meth:`_strip_slash_run` — the race turn is the direct-mode
        fan-out, the run turn is the direct-mode singleton.
        """
        if not text:
            return None
        if not text.startswith(_SLASH_RACE_PREFIX):
            return None
        return text[len(_SLASH_RACE_PREFIX):].strip()

    async def _prompt_thread_direct(
        self, session_id: str, text: str, *, recipe: str | None = None
    ) -> PromptResponse:
        """Direct-mode path: launch a fresh run + await its terminal status.

        Mirrors the run-session path (``launcher + _await_terminal``) but
        with the thread session's stored recipe. The new run id is a fresh
        mint; the thread session id is NOT a run id and is never passed to
        ``_reader`` / ``_stopper`` / ``_killer``.

        Zed S4 workspace handling: when ``cfg["workspace"] == "worktree"``
        and the project is a git repo, mint the worktree BEFORE launching
        and bind the new run id to the worktree path so ``_launch`` picks
        it up via ``self._sessions[new_run_id]``. A non-git project falls
        back to in-place (the user's checkout is unchanged). The run
        marker title gains the `` · worktree mini-ork/<run_id>`` suffix
        so the thread UI surfaces isolation at a glance.

        Z-W1: when the thread's cwd is a git linked worktree, the run
        **adopts** it (no new worktree is minted; the run commits onto
        the worktree's current branch). The marker suffix is
        `` · this worktree <branch>`` so the user sees the difference.
        """
        if not text:
            return PromptResponse(stop_reason="refusal")
        cfg = self._thread_config.get(session_id) or {}
        recipe = recipe or str(cfg.get("recipe") or self._recipe)
        new_run_id = mint_run_id()
        thread_cwd = self._sessions.get(session_id, os.getcwd())
        workspace_mode = str(
            cfg.get("workspace") or self._default_workspace or _WORKSPACE_WORKTREE
        )
        if workspace_mode not in _WORKSPACE_VALUES:
            workspace_mode = _WORKSPACE_WORKTREE
        ws_branch: str | None = None
        ws_adopted: bool = False
        run_cwd = thread_cwd
        if workspace_mode == _WORKSPACE_WORKTREE:
            from mini_ork import workspaces as _workspaces
            try:
                if (_workspaces.is_linked_worktree(Path(thread_cwd))
                        and not self.client_supports("fs.write")):
                    ws = _workspaces.adopt(
                        Path(thread_cwd), self._home_for(session_id), new_run_id
                    )
                    ws_adopted = True
                else:
                    task_label = _workspaces.task_name(
                        task_name_for_kickoff(text), new_run_id
                    )
                    ws = _workspaces.create(
                        Path(thread_cwd),
                        self._home_for(session_id),
                        new_run_id,
                        name=task_label,
                    )
                run_cwd = str(ws.path)
                ws_branch = ws.branch
            except RuntimeError:
                # Non-git project (or transient git error): stay in-place.
                workspace_mode = _WORKSPACE_IN_PLACE
        self._sessions[new_run_id] = run_cwd
        self._recipes[new_run_id] = recipe
        self._launch_count += 1
        launcher = self._launcher or self._launch
        result = launcher(new_run_id, text)
        self._launches[new_run_id] = result if isinstance(result, dict) else {}
        if isinstance(result, dict) and result.get("ok") is False:
            await self._emit(
                session_id,
                self._build_refusal_message(
                    f"mini-ork run failed to launch: {result.get('error') or 'unknown error'}"
                ),
            )
            # end_turn, not refusal: the thread goes on (Zed rewinds a refused prompt).
            return PromptResponse(stop_reason="end_turn")
        self._routes[new_run_id] = (session_id, f"{new_run_id}:")
        # Z5: track this run under the thread BEFORE the marker is emitted so
        # a re-load that re-emits the same marker finds the run already
        # recorded (``_load_thread_session`` rebuilds from the marker and
        # would dedupe-by-id).
        self._thread_runs.setdefault(session_id, []).append(new_run_id)
        await self._open_run_marker(
            session_id,
            new_run_id,
            recipe,
            worktree_branch=ws_branch,
            adopted=ws_adopted,
        )
        self._direct_runs[session_id] = new_run_id
        try:
            stop = await self._follow_in_thread(session_id, new_run_id)
        finally:
            self._direct_runs.pop(session_id, None)
        return PromptResponse(stop_reason="end_turn" if stop == "refusal" else stop)

    async def _prompt_thread_race(
        self, session_id: str, race_text: str
    ) -> PromptResponse:
        """``/race <task>`` turn: fan the thread's recipe across lanes.

        Thread-only (mirrors ``/run``'s gate); a run session returns
        ``"Races start in a mini-ork thread."``. Parses ``race_text`` via
        ``mini_ork.acp.race.parse_race_arg``; an error string is rendered
        verbatim and the turn ends. Otherwise: one workspace + one
        seeded run snapshot per lane, the runs launch with
        ``MO_ROUTING_POLICY=workflow_default`` so the race's lane pin
        survives a learning-governed router, follow concurrently, render
        the table, then ask the user to keep / later / discard.
        """
        if session_id not in self._thread_sessions:
            await self._emit(
                session_id,
                self._build_refusal_message("Races start in a mini-ork thread."),
            )
            return PromptResponse(stop_reason="end_turn")
        cfg = self._thread_config.get(session_id) or {}
        recipe = str(cfg.get("recipe") or self._recipe)
        home = self._home_for(session_id)
        thread_cwd = self._sessions.get(session_id) or os.getcwd()
        parsed = _race.parse_race_arg(race_text, home)
        if isinstance(parsed, str):
            await self._emit(session_id, self._build_refusal_message(parsed))
            return PromptResponse(stop_reason="end_turn")
        lanes, task = parsed
        if not task:
            await self._emit(
                session_id,
                self._build_refusal_message("What should they do? /race <task>"),
            )
            return PromptResponse(stop_reason="end_turn")
        from mini_ork import workspaces as _workspaces

        if not _workspaces._is_git_repo(Path(thread_cwd)):
            await self._emit(
                session_id,
                self._build_refusal_message(
                    "Racing needs a git project — each model works in its own worktree."
                ),
            )
            return PromptResponse(stop_reason="end_turn")

        n = len(lanes)
        first_message = (
            f"Racing {n} models on {recipe}: {', '.join(lanes)}. "
            "Each works in its own worktree; you keep one change. "
            f"It costs about {n}× one run."
        )
        await self._emit(
            session_id,
            AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=first_message),
            ),
        )

        # Per-lane launch state — one tuple per lane in declared order.
        # ``ws`` is None when the create/seeding step failed; ``run_id``
        # is the minted id (we keep it so the follow loop knows what to
        # drop from ``_skip_review`` / ``_run_env`` / ``_routes``).
        specs: list[tuple[str, str, _workspaces.Workspace | None, dict[str, Any] | None]] = []
        for i, lane in enumerate(lanes):
            rid = mint_run_id()
            title = f"race {i + 1}/{n} · {lane} — run {rid} ({recipe})"
            # Emit the marker before workspace create so a race whose
            # create raises still has a visible card to mark "failed".
            await self._emit(
                session_id,
                ToolCallStart(
                    session_update="tool_call",
                    tool_call_id=f"{rid}:parent",
                    title=title,
                    status="in_progress",
                    kind="other",
                ),
            )
            ws: _workspaces.Workspace | None = None
            try:
                ws = _workspaces.create(
                    Path(thread_cwd), home, rid,
                    name=_workspaces.task_name(f"{task_name_for_kickoff(task)} {lane}", rid),
                )
            except RuntimeError as exc:
                await self._emit(
                    session_id,
                    ToolCallProgress(
                        session_update="tool_call_update",
                        tool_call_id=f"{rid}:parent",
                        status="failed",
                    ),
                )
                await self._emit(
                    session_id,
                    AgentMessageChunk(
                        session_update="agent_message_chunk",
                        content=TextContentBlock(type="text", text=f"worktree create failed: {exc}"),
                    ),
                )
                specs.append((lane, rid, None, None))
                continue
            try:
                overlay = _race.seed_run_config(home, rid, recipe, lane)
            except Exception as exc:  # noqa: BLE001 — surface as a failed launch
                await self._emit(
                    session_id,
                    ToolCallProgress(
                        session_update="tool_call_update",
                        tool_call_id=f"{rid}:parent",
                        status="failed",
                    ),
                )
                await self._emit(
                    session_id,
                    AgentMessageChunk(
                        session_update="agent_message_chunk",
                        content=TextContentBlock(type="text", text=f"seed_run_config failed: {exc}"),
                    ),
                )
                if ws is not None:
                    try:
                        _workspaces.discard(ws)
                    except Exception:
                        pass
                specs.append((lane, rid, None, None))
                continue
            self._sessions[rid] = str(ws.path)
            self._recipes[rid] = recipe
            self._skip_review.add(rid)
            # Race's pin survives learning-governed routing: workflow_default
            # does NOT remap unpinned nodes, so the seeded implementer lane
            # is the one the run dispatches on.
            self._run_env[rid] = {"MO_ROUTING_POLICY": "workflow_default",
                                  "MINI_ORK_AGENTS": str(overlay)}
            self._launch_count += 1
            launcher = self._launcher or self._launch
            try:
                result = launcher(rid, task)
            except Exception as exc:  # noqa: BLE001 — the seam itself blew up
                result = {"ok": False, "error": str(exc)}
            self._launches[rid] = result if isinstance(result, dict) else {}
            if not isinstance(result, dict) or result.get("ok") is False:
                err = (result.get("error") if isinstance(result, dict) else None) or "unknown error"
                await self._emit(
                    session_id,
                    ToolCallProgress(
                        session_update="tool_call_update",
                        tool_call_id=f"{rid}:parent",
                        status="failed",
                    ),
                )
                await self._emit(
                    session_id,
                    AgentMessageChunk(
                        session_update="agent_message_chunk",
                        content=TextContentBlock(type="text", text=f"launch failed: {err}"),
                    ),
                )
                if ws is not None:
                    try:
                        _workspaces.discard(ws)
                    except Exception:
                        pass
                self._run_env.pop(rid, None)
                self._skip_review.discard(rid)
                specs.append((lane, rid, None, None))
                continue
            self._routes[rid] = (session_id, f"{rid}:")
            self._thread_runs.setdefault(session_id, []).append(rid)
            # Update the marker title with the branch suffix; the first
            # emission lacked it because ``ws`` was created inside this
            # loop. ``_open_run_marker``'s pattern is reused.
            await self._emit(
                session_id,
                ToolCallStart(
                    session_update="tool_call",
                    tool_call_id=f"{rid}:parent",
                    title=f"{title} · worktree {ws.branch}",
                    status="in_progress",
                    kind="other",
                ),
            )
            specs.append((lane, rid, ws, result))

        # Concurrent follow over the runs that actually launched.
        launchable = [s for s in specs if s[2] is not None and s[3] is not None]
        self._race_runs[session_id] = [s[1] for s in launchable]
        try:
            if launchable:
                await asyncio.gather(
                    *(self._await_terminal(s[1]) for s in launchable),
                    return_exceptions=False,
                )
        finally:
            self._race_runs.pop(session_id, None)
        if session_id in self._cancelled:
            # The user stopped the turn; cancel() already stopped every run.
            self._race_cleanup(specs)
            return PromptResponse(stop_reason="cancelled")

        # Close markers per spec — completed / failed, no per-run S5
        # buttons (handled via ``_skip_review`` already).
        for lane, rid, ws, result in launchable:
            ok = self._run_status.get(rid) == "published"
            await self._emit(
                session_id,
                ToolCallProgress(
                    session_update="tool_call_update",
                    tool_call_id=f"{rid}:parent",
                    status="completed" if ok else "failed",
                ),
            )

        # Build rows for the table — cost / change / time per run.
        rows = self._collect_race_rows(specs)
        table_text = _race.render_race_table(rows)
        await self._emit(
            session_id,
            AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=table_text),
            ),
        )

        # Decision: candidates = published + worktree + change > 0.
        candidates = self._race_candidates(specs)
        if not candidates:
            await self._emit(
                session_id,
                self._build_refusal_message(
                    "No model produced a verified change. Their worktrees are kept for "
                    "a look: /workspaces lists them, /discard <run> removes one."
                ),
            )
            self._race_cleanup(specs)
            return PromptResponse(stop_reason="end_turn")

        # Surface the keep / later / discard_all permission card. The
        # tool_call_id must NOT collide with any per-run ``<rid>:parent``
        # marker; ``race:<first_rid>`` is unique per race.
        first_rid = candidates[0][1]
        options = [
            PermissionOption(
                option_id=f"keep:{rid}",
                name=self._race_keep_label(lane, rid),
                kind="allow_once",
            )
            for lane, rid, _ws in candidates
        ]
        options.append(
            PermissionOption(option_id="later", name="Decide later", kind="reject_once")
        )
        options.append(
            PermissionOption(option_id="discard_all", name="Discard all", kind="reject_always")
        )
        if self._conn is not None:
            try:
                response = await self._conn.request_permission(
                    session_id=session_id,
                    tool_call=ToolCallUpdate(
                        tool_call_id=f"race:{first_rid}",
                        kind="edit",
                        title="Pick the change to keep",
                        status="pending",
                    ),
                    options=options,
                )
                outcome = getattr(response, "outcome", None)
                option_id = getattr(outcome, "option_id", None) if outcome is not None else None
            except Exception:  # noqa: BLE001 — UI is best-effort
                option_id = None
        else:
            option_id = None
        await self._handle_race_decision(session_id, specs, candidates, option_id)
        return PromptResponse(stop_reason="end_turn")

    def _collect_race_rows(
        self,
        specs: list[tuple[str, str, Any, dict[str, Any] | None]],
    ) -> list[dict[str, Any]]:
        """One row per lane with the table fields populated.

        Cost = ``llm_calls.cost_usd`` summed in the run's snapshot.
        Time = first-event ``ts`` minus last-event ``ts`` (the
        snapshot's events list carries ts). Change = ``workspaces.status``
        (added/removed). Status = published/failed/rolled_back/``running``.
        """
        from mini_ork import workspaces as _workspaces

        reader = self._reader or self._read_snapshot
        out: list[dict[str, Any]] = []
        for lane, rid, ws, _result in specs:
            snap = reader(rid) if ws is not None else None
            if ws is None:
                out.append({
                    "lane": lane,
                    "run_id": rid,
                    "status": "failed",
                    "added": 0,
                    "removed": 0,
                    "cost_usd": 0.0,
                    "seconds": 0,
                })
                continue
            status = _race.result_word((snap or {}).get("status"))
            cost = self._race_cost(rid)
            events = (snap or {}).get("events") or []
            seconds = 0
            if events:
                try:
                    first_ts = int(events[0].get("ts") or events[0].get("created_at") or 0)
                    last_ts = int(events[-1].get("ts") or events[-1].get("created_at") or 0)
                    seconds = max(0, last_ts - first_ts)
                except (TypeError, ValueError):
                    seconds = 0
            try:
                ws_status = _workspaces.status(ws)
                added = int(ws_status.get("added") or 0)
                removed = int(ws_status.get("removed") or 0)
            except Exception:
                added, removed = 0, 0
            out.append({
                "lane": lane,
                "run_id": rid,
                "status": status,
                "added": added,
                "removed": removed,
                "cost_usd": cost,
                "seconds": seconds,
            })
        return out

    def _race_candidates(
        self, specs: list[tuple[str, str, Any, dict[str, Any] | None]]
    ) -> list[tuple[str, str, Any]]:
        """Runs whose task state is ready to review (published + changes).

        Mirrors :func:`_offer_run_review`'s filter — the same condition
        that would otherwise raise the per-run S5 buttons. The race's
        parent permission owns the decision; no per-run buttons fire
        (``_skip_review``).
        """
        from mini_ork import workspaces as _workspaces

        candidates: list[tuple[str, str, Any]] = []
        for lane, rid, ws, _result in specs:
            if ws is None:
                continue
            if self._run_status.get(rid) != "published":
                continue
            run_dir = self._home_for(rid) / "runs" / rid
            if not run_dir.is_dir():
                continue
            snap = (self._reader or self._read_snapshot)(rid) or {}
            ts = _task_state.task_state(run_dir, snap)
            if ts.state != "needs_you" or not ts.detail.startswith("Ready to review:"):
                continue
            try:
                ws_status = _workspaces.status(ws)
            except Exception:
                ws_status = {}
            if int(ws_status.get("added") or 0) + int(ws_status.get("removed") or 0) == 0:
                continue
            candidates.append((lane, rid, ws))
        return candidates

    def _race_keep_label(self, lane: str, rid: str) -> str:
        """``Keep <lane> (+a −r, $cost)`` — the per-candidate button name.

        Reads the workspace status to fill the +/- change gauge; the
        cost gauge needs the task_runs row (the same source ``/status``
        reads). Returns a sane default when either probe fails so the
        permission card is never a blank string.
        """
        from mini_ork import workspaces as _workspaces

        home = self._home_for(rid)
        ws_obj = _workspaces.load(home, rid)
        added = removed = 0
        if ws_obj is not None:
            try:
                ws_status = _workspaces.status(ws_obj)
                added = int(ws_status.get("added") or 0)
                removed = int(ws_status.get("removed") or 0)
            except Exception:
                pass
        cost = self._race_cost(rid)
        change = f"+{added} −{removed}" if (added or removed) else "—"
        cost_str = f"${cost:.2f}" if cost > 0 else "—"
        return f"Keep {lane} ({change}, {cost_str})"

    def _race_cost(self, run_id: str) -> float:
        """The run's spend as ``/status`` computes it (sum of llm_calls)."""
        try:
            from mini_ork.web.deps import db_for
            from mini_ork.web.repositories import RunDetailRepository

            row = RunDetailRepository(db_for(self._home_for(run_id))).fetch_task_run_row(run_id)
            if row is not None:
                return float(row.get("cost_usd") or 0.0)
        except Exception:
            pass
        try:
            snap = (self._reader or self._read_snapshot)(run_id) or {}
            return sum(float(c.get("cost_usd") or 0.0) for c in (snap.get("llm_calls") or []))
        except Exception:
            return 0.0

    async def _handle_race_decision(
        self,
        session_id: str,
        specs: list[tuple[str, str, Any, dict[str, Any] | None]],
        candidates: list[tuple[str, str, Any]],
        option_id: str | None,
    ) -> None:
        """Apply the user's pick: keep → merge + discard the rest; later /
        discard_all → the kickoff's verbatim messages."""
        from mini_ork import workspaces as _workspaces

        if option_id and option_id.startswith("keep:"):
            target_rid = option_id[len("keep:"):]
            keep_lane = ""
            keep_ws = None
            for lane, rid, ws in candidates:
                if rid == target_rid:
                    keep_lane, keep_ws = lane, ws
                    break
            if keep_ws is None:
                await self._emit(
                    session_id,
                    self._build_refusal_message(
                        "Nothing was discarded."
                    ),
                )
                self._race_cleanup(specs)
                return
            base = (
                self._thread_titles.get(session_id)
                or self._run_base_titles.get(target_rid)
                or ""
            )
            title = f"{base} (mini-ork race, {keep_lane})" if base else f"mini-ork race, {keep_lane}"
            try:
                result = _workspaces.merge(keep_ws, message=title)
            except Exception as exc:  # noqa: BLE001
                await self._emit(
                    session_id,
                    self._build_refusal_message(
                        f"Merge refused: {exc}. Nothing was discarded."
                    ),
                )
                self._race_cleanup(specs)
                return
            if not result.get("ok"):
                err = (result.get("error") if isinstance(result, dict) else None) or "unknown error"
                await self._emit(
                    session_id,
                    self._build_refusal_message(
                        f"Merge refused: {err}. Nothing was discarded."
                    ),
                )
                self._race_cleanup(specs)
                return
            merged_sha = str(result.get("merged") or "")[:7]
            mode = str(result.get("mode") or "merge")
            wording = "fast-forward" if mode == "fast-forward" else "merge commit"
            sha = merged_sha or "—"
            others = [
                (lane, rid, ws) for lane, rid, ws, _result in specs
                if ws is not None and rid != target_rid
            ]
            for _lane, _rid, ws in others:
                try:
                    _workspaces.discard(ws)
                except Exception:
                    pass
            await self._emit(
                session_id,
                self._build_refusal_message(
                    f"Merged {keep_lane}'s change into {keep_ws.base_branch} "
                    f"({wording} {sha}). Discarded the other {len(others)}."
                ),
            )
            self._race_cleanup(specs)
            return
        if option_id == "later":
            n = sum(1 for s in specs if s[2] is not None)
            await self._emit(
                session_id,
                self._build_refusal_message(
                    f"All {n} kept: /merge <run> keeps one, /discard <run> drops one."
                ),
            )
            self._race_cleanup(specs)
            return
        if option_id == "discard_all":
            for _lane, _rid, ws, _result in specs:
                if ws is None:
                    continue
                try:
                    _workspaces.discard(ws)
                except Exception:
                    pass
            n = sum(1 for s in specs if s[2] is not None)
            await self._emit(
                session_id,
                self._build_refusal_message(f"Discarded all {n}."),
            )
            self._race_cleanup(specs)
            return
        # Dismissed / unknown → behave like ``later``.
        n = sum(1 for s in specs if s[2] is not None)
        await self._emit(
            session_id,
            self._build_refusal_message(
                f"All {n} kept: /merge <run> keeps one, /discard <run> drops one."
            ),
        )
        self._race_cleanup(specs)
        return

    def _race_cleanup(
        self, specs: list[tuple[str, str, Any, dict[str, Any] | None]]
    ) -> None:
        """Drop the per-run state the race injected (``_run_env``,
        ``_skip_review``, ``_thread_runs`` entries for race-only runs)."""
        keep = set()
        for _lane, rid, ws, _result in specs:
            if ws is None:
                # Even failed launches had a row in _thread_runs? They
                # don't — failed launches skip the _routes + _thread_runs
                # append, so this is just a guard.
                self._run_env.pop(rid, None)
                self._skip_review.discard(rid)
                continue
            keep.add(rid)
            self._run_env.pop(rid, None)
            self._skip_review.discard(rid)

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
        # S3b-2: this turn's draft_recipe calls, and the last successful draft
        # (approval buttons are offered only for a draft that exists).
        draft_calls: set[str] = set()
        turn_draft: dict[str, Any] = {}
        # Zed S6b-2: this turn's propose_automation calls, and the last
        # successful proposal. Mirror of ``draft_calls`` / ``turn_draft`` for
        # the scheduling flow; the approval buttons fire only when a
        # proposal exists (same shape as recipe drafts).
        propose_calls: set[str] = set()
        turn_proposal: dict[str, Any] = {}
        # Zed S7b: this turn's draft_kickoff calls (tool_use id set) plus a
        # side-table of the kickoff markdown + recipe the orchestrator
        # submitted (captured from the tool_use input — the tool_result
        # does not carry the markdown). The last successful kickoff drives
        # the 4-button approval (Start run / Save / Change / Discard).
        kickoff_calls: set[str] = set()
        kickoff_inputs: dict[str, dict[str, str]] = {}
        turn_kickoff: dict[str, Any] = {}

        async def on_event(event: dict[str, Any]) -> None:
            for update in _orchestration.map_event(event):
                await self._emit(session_id, update)
            if event.get("type") == "assistant":
                for block in (event.get("message") or {}).get("content") or []:
                    if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                        continue
                    tool = str(block.get("name") or "").split("__")[-1]
                    if tool == _orchestration.CHILD_RUN_TOOL:
                        inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                        start_run_calls[str(block.get("id") or "")] = str(inp.get("recipe") or "")
                    elif tool == "draft_recipe":
                        draft_calls.add(str(block.get("id") or ""))
                    elif tool == "propose_automation":
                        propose_calls.add(str(block.get("id") or ""))
                    elif tool == "draft_kickoff":
                        kid = str(block.get("id") or "")
                        if not kid:
                            continue
                        kickoff_calls.add(kid)
                        inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                        kickoff_inputs[kid] = {
                            "markdown": str(inp.get("kickoff_markdown") or ""),
                            "recipe": str(inp.get("recipe") or ""),
                        }
            child = self._extract_child_run_from_event(event, start_run_calls)
            if child:
                await self._start_child_follow(session_id, child[0], recipe=child[1])
            draft = _draft_result(event, draft_calls)
            if draft:
                turn_draft.clear()
                turn_draft.update(draft)
                await self._emit_draft_preview(session_id, home, draft)
            proposal = _proposal_result(event, propose_calls)
            if proposal:
                turn_proposal.clear()
                turn_proposal.update(proposal)
                await self._emit_proposal_card(session_id, proposal)
            kickoff = _kickoff_result(event, kickoff_calls)
            if kickoff:
                inputs = kickoff_inputs.get(kickoff["tool_call_id"], {})
                kickoff["markdown"] = inputs.get("markdown", "")
                if not kickoff.get("recipe"):
                    kickoff["recipe"] = inputs.get("recipe", "")
                if kickoff.get("ok"):
                    turn_kickoff.clear()
                    turn_kickoff.update(kickoff)
                    await self._emit_kickoff_preview(session_id, kickoff)

        async def _run() -> Any:
            kwargs: dict[str, Any] = {}
            # Zed S4: pass workspace_mode to the harness so the spawned MCP
            # server inside the orchestrator subprocess defaults to the
            # thread's mode. Test-injected ``turn`` fakes use a fixed
            # signature, so only forward when we're calling our own default.
            if turn is self._default_orchestrator_turn:
                kwargs["workspace_mode"] = str(cfg.get("workspace") or _WORKSPACE_WORKTREE)
            return await turn(
                lane=lane,
                prompt=text,
                cwd=Path(cwd),
                home=home,
                resume=resume,
                on_event=on_event,
                **kwargs,
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
            # Z9c-2: record the resume id ONLY when it changes (kickoff:
            # "when the stored id changes"). The first turn is also a change
            # because the prior value was empty.
            if cfg.get("claude_session_id") != new_session_id:
                self._record(session_id, {"type": "claude_session", "id": new_session_id})
            cfg["claude_session_id"] = new_session_id
        costs = self._thread_costs.setdefault(session_id, {})
        costs["orchestrator"] = costs.get("orchestrator", 0.0) + float(
            getattr(result, "cost_usd", 0.0) or 0.0
        )
        await self._emit(session_id, self._thread_usage_update(session_id))
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
        # S3b-2: if this thread is mid-drafting, surface the approval
        # buttons BEFORE we end the turn. The user's Create / Change /
        # Discard choice is awaited inline (ACP ``request_permission``) so
        # the turn does not return until the user has decided. Failed
        # orchestrator turns (``rc != 0``) skip the approval — we want the
        # error text to be the last thing rendered, not a Create dialog.
        # Zed S6b-2: a recipe draft is offered before an automation proposal
        # from the same turn — the automation may run that very recipe, which
        # must exist before the automation can be created.
        if turn_draft:
            await self._offer_recipe_draft_approval(
                session_id, turn_draft["draft_id"],
                tool_call_id=turn_draft["tool_call_id"], exists=turn_draft["exists"],
            )
        if turn_proposal:
            await self._offer_automation_proposal_approval(
                session_id, turn_proposal["proposal_id"],
                tool_call_id=turn_proposal["tool_call_id"],
                exists=turn_proposal["exists"],
            )
        # Zed S7b: kickoff approval fires LAST so a kickoff that triggers
        # a child run (via the ``run`` option) gets its workspace / lane
        # decisions made after the recipe/proposal drafts of the same turn
        # have settled. An ``ok: false`` kickoff is silently ignored per
        # the kickoff §Agent spec.
        if turn_kickoff and turn_kickoff.get("ok"):
            await self._offer_kickoff_draft_approval(
                session_id, turn_kickoff["draft_id"],
                tool_call_id=turn_kickoff["tool_call_id"],
                recipe=str(turn_kickoff.get("recipe") or ""),
            )
        # Zed S5: surface review buttons for any run that became
        # ready-to-review during this turn, newest first. The orchestrator
        # task is already popped (we are about to return ``end_turn``) so
        # the active-turn guard inside ``_offer_run_review`` would normally
        # fall through to the one-time message — the explicit
        # ``force=True`` here re-asserts the active-turn semantics for
        # this final pass.
        await self._offer_thread_review(session_id)
        return PromptResponse(stop_reason="end_turn")

    # ── recipe-draft approval (Z9c-2 / S3b-2) ─────────────────────────────────

    async def _offer_recipe_draft_approval(
        self, session_id: str, recipe_id: str, *, tool_call_id: str, exists: bool
    ) -> None:
        """Ask the user, on the draft's own tool card, what to do with it:
        Create (or Update) recipe / Change something / Discard draft. Awaited
        inside the turn. Dismissing keeps the draft."""
        if self._conn is None:
            return
        verb = "Update" if exists else "Create"
        try:
            response = await self._conn.request_permission(
                session_id=session_id,
                tool_call=ToolCallUpdate(
                    tool_call_id=tool_call_id,
                    kind="edit",
                    title=f"{verb} recipe {recipe_id}",
                    status="pending",
                ),
                options=[
                    PermissionOption(option_id="create", name=f"{verb} recipe", kind="allow_once"),
                    PermissionOption(option_id="change", name="Change something", kind="reject_once"),
                    PermissionOption(option_id="discard", name="Discard draft", kind="reject_always"),
                ],
            )
        except Exception:  # noqa: BLE001 — UI is best-effort, the draft survives
            return
        outcome = getattr(response, "outcome", None)
        option_id = getattr(outcome, "option_id", None) if outcome is not None else None
        if option_id == "create":
            await self._handle_recipe_create(session_id, recipe_id)
        elif option_id == "change":
            await self._handle_recipe_change(session_id, recipe_id)
        elif option_id == "discard":
            await self._handle_recipe_discard(session_id, recipe_id)
        else:  # dismissed
            await self._emit(session_id, self._build_refusal_message(
                "Draft kept — ask me to create it when you're ready."))

    async def _emit_draft_preview(self, session_id: str, home: Path, draft: dict[str, Any]) -> None:
        """Show a draft on its tool card: every file as a diff against what is
        in the project now (new files have no old text), then its grade.
        Files are read from the draft directory, not from the tool result."""
        rid = draft["draft_id"]
        draft_dir = home / "recipe-drafts" / rid
        target = home / "recipes" / rid
        content: list[Any] = []
        for path in sorted(p for p in draft_dir.rglob("*") if p.is_file()):
            rel = path.relative_to(draft_dir).as_posix()
            if rel in ("draft.json", "recipe.spec.json"):
                continue
            try:
                new_text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            old_path = target / rel
            old_text = None
            if old_path.is_file():
                try:
                    old_text = old_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    old_text = None
            content.append(FileEditToolCallContent(
                type="diff", path=str(old_path.resolve()), old_text=old_text, new_text=new_text))
        grade = draft.get("grade") or {}
        lines = [f"Grade {grade.get('letter', '?')} ({grade.get('score', '?')}/100)"]
        for w in (draft.get("warnings") or []) + (grade.get("findings") or []):
            if isinstance(w, dict) and w.get("msg"):
                lines.append(f"- {w['msg']}" + (f" — {w['fix']}" if w.get("fix") else ""))
        content.append(ContentToolCallContent(
            type="content", content=TextContentBlock(type="text", text="\n".join(lines))))
        await self._emit(session_id, ToolCallProgress(
            session_update="tool_call_update", tool_call_id=draft["tool_call_id"],
            content=cast(Any, content)))

    # ── automation-proposal approval (Zed S6b-2) ─────────────────────────────

    async def _emit_proposal_card(
        self, session_id: str, proposal: dict[str, Any]
    ) -> None:
        """Show a proposal on its tool card — three short lines + the kickoff.

        Card body (kickoff §Proposal card, verbatim):

            **<name>** runs `<recipe>` <when> (`<cron>`), <workspace>.
            Next: <three next_fires formatted like /automations>
            ```markdown
            <first 40 lines of kickoff, ``…`` when longer>
            ```
        """
        from mini_ork.acp import automation_view as _av
        now = _dt.datetime.now()
        next_lines: list[str] = []
        for raw in (proposal.get("next_fires") or [])[:3]:
            try:
                when = _dt.datetime.fromisoformat(str(raw))
            except (TypeError, ValueError):
                continue
            next_lines.append(_av._format_next_fire(when, now))
        next_text = ", ".join(next_lines) if next_lines else "(no future fire)"
        workspace_text = (
            "in a new worktree"
            if proposal.get("workspace", "worktree") == "worktree"
            else "in place"
        )
        cron = str(proposal.get("schedule") or "")
        when_text = str(proposal.get("when") or "") or cron
        lines = [
            f"**{proposal.get('name', proposal.get('proposal_id', ''))}** "
            f"runs `{proposal.get('recipe', '')}` {when_text} (`{cron}`), "
            f"{workspace_text}.",
            f"Next: {next_text}",
        ]
        kickoff_lines = str(proposal.get("kickoff") or "").splitlines()[:40]
        kickoff_block = "\n".join(kickoff_lines)
        if len(str(proposal.get("kickoff") or "").splitlines()) > 40:
            kickoff_block += "\n…"
        lines.append("```markdown\n" + kickoff_block + "\n```")
        await self._emit(session_id, ToolCallProgress(
            session_update="tool_call_update",
            tool_call_id=proposal["tool_call_id"],
            content=[ContentToolCallContent(
                type="content",
                content=TextContentBlock(type="text", text="\n\n".join(lines)),
            )],
        ))

    async def _offer_automation_proposal_approval(
        self, session_id: str, proposal_id: str, *, tool_call_id: str, exists: bool
    ) -> None:
        """Ask the user, on the proposal's own tool card, what to do with it:
        Create (or Update) automation / Change something / Discard. Awaited
        inside the turn. Dismissing keeps the proposal.

        On ``create``, after :func:`mini_ork.automations.commit_proposal`
        succeeds, when the OS scheduler is OFF we emit a SECOND
        ``request_permission`` on a fresh tool_call id
        (``automation-scheduler:<home hash>``) so an automation that
        never fires is not the worst outcome.
        """
        if self._conn is None:
            return
        verb = "Update" if exists else "Create"
        try:
            response = await self._conn.request_permission(
                session_id=session_id,
                tool_call=ToolCallUpdate(
                    tool_call_id=tool_call_id,
                    kind="edit",
                    title=f"{verb} automation {proposal_id}",
                    status="pending",
                ),
                options=[
                    PermissionOption(option_id="create", name=f"{verb} automation", kind="allow_once"),
                    PermissionOption(option_id="change", name="Change something", kind="reject_once"),
                    PermissionOption(option_id="discard", name="Discard", kind="reject_always"),
                ],
            )
        except Exception:  # noqa: BLE001 — UI is best-effort, the proposal survives
            return
        outcome = getattr(response, "outcome", None)
        option_id = getattr(outcome, "option_id", None) if outcome is not None else None
        if option_id == "create":
            await self._handle_automation_proposal_create(
                session_id, proposal_id, tool_call_id=tool_call_id
            )
        elif option_id == "change":
            await self._handle_automation_proposal_change(session_id, proposal_id)
            await self._emit(session_id, ToolCallProgress(
                session_update="tool_call_update",
                tool_call_id=tool_call_id,
                status="failed",
            ))
        elif option_id == "discard":
            await self._handle_automation_proposal_discard(session_id, proposal_id)
            await self._emit(session_id, ToolCallProgress(
                session_update="tool_call_update",
                tool_call_id=tool_call_id,
                status="failed",
            ))
        else:  # dismissed
            await self._emit(session_id, self._build_refusal_message(
                "Proposal kept — ask me to create it when you're ready."))
            await self._emit(session_id, ToolCallProgress(
                session_update="tool_call_update",
                tool_call_id=tool_call_id,
                status="failed",
            ))

    async def _handle_automation_proposal_create(
        self, session_id: str, proposal_id: str, *, tool_call_id: str
    ) -> None:
        """Commit the staged proposal. On success: mark the tool call
        completed and (when the OS scheduler is OFF) ask the user to
        install it. On failure: emit the error and leave the proposal
        alive on disk.
        """
        from mini_ork import automations as _auto

        home = self._home_for(session_id)
        try:
            result = _auto.commit_proposal(home, proposal_id)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": str(exc)}
        ok = bool(result.get("ok")) if isinstance(result, dict) else False
        if not ok:
            err = (
                (result.get("error") if isinstance(result, dict) else None)
                or "unknown error"
            )
            await self._emit(
                session_id,
                self._build_refusal_message(
                    f"Could not create {proposal_id}: {err} The proposal is kept."
                ),
            )
            await self._emit(session_id, ToolCallProgress(
                session_update="tool_call_update",
                tool_call_id=tool_call_id,
                status="failed",
            ))
            return
        record = result.get("automation") if isinstance(result, dict) else None
        name = (
            (record or {}).get("name")
            if isinstance(record, dict)
            else None
        ) or proposal_id
        schedule = (
            (record or {}).get("schedule")
            if isinstance(record, dict)
            else None
        ) or ""
        # ``commit_proposal`` returns ``created=True`` for fresh adds,
        # ``created=False`` when the proposal updates an existing
        # automation.
        verb = "Updated" if not result.get("created") else "Scheduled"
        # Next-run text: re-read the proposal's next_fires for a fresh line
        # (the live store has been updated with the proposal's schedule).
        next_text = ""
        try:
            items = _auto.load(home)
            live = next((a for a in items if a.get("id") == proposal_id), None)
            if live:
                from mini_ork.acp import automation_view as _av
                now = _dt.datetime.now()
                fires = _auto.next_fires(str(live.get("schedule") or ""), n=1, after=now)
                if fires:
                    next_text = _av._format_next_fire(fires[0], now)
        except Exception:  # noqa: BLE001 — non-essential follow-up
            next_text = ""
        when_text = _auto.describe(schedule) if schedule else ""
        when_part = f": {when_text}" if when_text else ""
        next_part = f" Next run {next_text}." if next_text else ""
        await self._emit(
            session_id,
            self._build_refusal_message(f"{verb} {name}{when_part}.{next_part}"),
        )
        await self._emit(session_id, ToolCallProgress(
            session_update="tool_call_update",
            tool_call_id=tool_call_id,
            status="completed",
        ))
        # Offer to install the scheduler when it is OFF. A separate
        # ``request_permission`` on a fresh tool_call id, NOT the proposal
        # id — reusing the proposal id would overwrite the in-flight card.
        try:
            status = _auto.scheduler_status(home)
        except Exception:  # noqa: BLE001
            status = {"installed": True}  # safest default: don't ask
        if not bool(status.get("installed")):
            await self._offer_automation_scheduler_on(
                session_id, home, proposal_name=name
            )

    async def _handle_automation_proposal_change(
        self, session_id: str, proposal_id: str
    ) -> None:
        """Keep the proposal; the user's next message (what to change) goes to
        the orchestrator as typed — its conversation already holds it."""
        del proposal_id
        await self._emit(session_id, self._build_refusal_message(
            "Tell me what to change."
        ))

    async def _handle_automation_proposal_discard(
        self, session_id: str, proposal_id: str
    ) -> None:
        """User picked Discard: drop the staged proposal and report."""
        from mini_ork import automations as _auto

        home = self._home_for(session_id)
        try:
            result = _auto.discard_proposal(home, proposal_id)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": str(exc)}
        if isinstance(result, dict) and result.get("ok"):
            await self._emit(
                session_id,
                self._build_refusal_message("Proposal discarded."),
            )
        else:
            err = (
                (result.get("error") if isinstance(result, dict) else None)
                or "unknown error"
            )
            await self._emit(
                session_id,
                self._build_refusal_message(
                    f"`discard` failed for `{proposal_id}`: {err}"
                ),
            )

    async def _offer_automation_scheduler_on(
        self, session_id: str, home: Path, *, proposal_name: str
    ) -> None:
        """Second question after ``create`` succeeds: turn the OS scheduler
        ON so the automation actually fires when the editor is closed.

        Uses a fresh tool_call id (``automation-scheduler:<home hash>``)
        so the in-flight proposal card is not overwritten. ``on`` calls
        :func:`mini_ork.automations.install_scheduler`; ``later`` /
        dismissed explain that the automation is saved but inert.
        """
        from mini_ork import automations as _auto

        del proposal_name
        if self._conn is None:
            return
        tool_call_id = f"automation-scheduler:{_auto._home_hash(home)}"
        await self._emit(
            session_id,
            self._build_refusal_message(
                "Automations fire from a small background job that checks "
                "every minute — a LaunchAgent on macOS, a crontab line on "
                "Linux — even when Zed is closed. It is off in this project."
            ),
        )
        await self._emit(session_id, ToolCallStart(
            session_update="tool_call", tool_call_id=tool_call_id,
            kind="execute", status="pending", title="Turn on the scheduler",
        ))
        option_id = None
        try:
            response = await self._conn.request_permission(
                session_id=session_id,
                tool_call=ToolCallUpdate(tool_call_id=tool_call_id, status="pending"),
                options=[
                    PermissionOption(option_id="on", name="Turn on the scheduler", kind="allow_once"),
                    PermissionOption(option_id="later", name="Not now", kind="reject_once"),
                ],
            )
            outcome = getattr(response, "outcome", None)
            option_id = getattr(outcome, "option_id", None) if outcome is not None else None
        except Exception:  # noqa: BLE001 — non-essential second question
            option_id = None
        if option_id == "on":
            try:
                result = _auto.install_scheduler(home)
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "error": str(exc)}
            if isinstance(result, dict) and result.get("ok"):
                await self._emit(
                    session_id,
                    self._build_refusal_message(
                        "Scheduler on — /automation scheduler off removes it."
                    ),
                )
            else:
                err = (
                    (result.get("error") if isinstance(result, dict) else None)
                    or "unknown error"
                )
                await self._emit(
                    session_id,
                    self._build_refusal_message(
                        f"Could not turn the scheduler on: {err}"
                    ),
                )
            await self._emit(session_id, ToolCallProgress(
                session_update="tool_call_update",
                tool_call_id=tool_call_id,
                status="completed",
            ))
            return
        # ``later`` or dismissed — automation saved but inert.
        await self._emit(session_id, self._build_refusal_message(
            "Saved, but it will not fire until the scheduler is on: "
            "/automation scheduler on."
        ))
        await self._emit(session_id, ToolCallProgress(
            session_update="tool_call_update",
            tool_call_id=tool_call_id,
            status="failed",
        ))

    async def _handle_recipe_create(
        self, session_id: str, recipe_id: str
    ) -> None:
        """Commit the staged draft and surface a Create / Discard / Run test
        ``request_permission`` to the client. ``commit_draft`` writes the
        recipe tree to ``.mini-ork/recipes/<id>/``; on failure we report
        the error and leave the draft alone (no rollback).
        """
        from mini_ork.recipe_author import commit_draft

        home = self._home_for(session_id)
        try:
            result = commit_draft(home, recipe_id)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": str(exc)}
        ok = bool(result.get("ok")) if isinstance(result, dict) else False
        if not ok:
            err = (
                (result.get("error") if isinstance(result, dict) else None)
                or "unknown error"
            )
            await self._emit(
                session_id,
                self._build_refusal_message(
                    f"`create` failed for `{recipe_id}`: {err}"
                ),
            )
            return
        target = home / "recipes" / recipe_id
        backup = result.get("backup") if isinstance(result, dict) else None
        msg = (
            f"{'Updated' if backup else 'Created'} `.mini-ork/recipes/{recipe_id}` — "
            "it is in the Recipe picker now."
            + (f" The previous version is in `{backup}`." if backup else "")
        )
        await self._emit(session_id, self._build_refusal_message(msg))
        await self._emit_file_links(session_id, sorted(
            p for p in target.rglob("*")
            if p.is_file() and p.name != "recipe.spec.json"
        ))
        await self._offer_recipe_test_run(session_id, recipe_id)

    async def _handle_recipe_change(self, session_id: str, recipe_id: str) -> None:
        """Keep the draft; the user's next message (what to change) goes to the
        orchestrator as typed — its conversation already holds the draft."""
        del recipe_id
        await self._emit(session_id, self._build_refusal_message("Tell me what to change."))

    async def _handle_recipe_discard(
        self, session_id: str, recipe_id: str
    ) -> None:
        """User picked Drop the draft via ``discard_draft`` and report."""
        from mini_ork.recipe_author import discard_draft

        home = self._home_for(session_id)
        try:
            result = discard_draft(home, recipe_id)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": str(exc)}
        ok = bool(result.get("ok")) if isinstance(result, dict) else False
        if ok:
            await self._emit(
                session_id,
                self._build_refusal_message(
                    f"Recipe `{recipe_id}` draft discarded."
                ),
            )
        else:
            err = (
                (result.get("error") if isinstance(result, dict) else None)
                or "unknown error"
            )
            await self._emit(
                session_id,
                self._build_refusal_message(
                    f"`discard` failed for `{recipe_id}`: {err}"
                ),
            )

    # ── kickoff-draft approval (Zed S7b) ──────────────────────────────────────

    async def _emit_kickoff_preview(
        self, session_id: str, kickoff: dict[str, Any]
    ) -> None:
        """Show a kickoff draft on its tool card: a new-file diff with the
        markdown preview, then one line per finding (or "Looks complete.").

        Card body (kickoff §Agent, verbatim):

            FileEditToolCallContent(type="diff", path=<home>/kickoffs/<slug>.md,
                                    old_text=None, new_text=<markdown>)
            text:
              - "Looks complete." when there are no findings
              - one line per finding ("⚠ msg — fix" for warn,
                "✗ msg — fix" for error) otherwise
        """
        home = self._home_for(session_id)
        slug = str(kickoff.get("draft_id") or "kickoff")
        path = str(home / "kickoffs" / f"{slug}.md")
        findings = kickoff.get("findings") or []
        if findings:
            lines: list[str] = []
            for f in findings:
                if not isinstance(f, dict):
                    continue
                glyph = "✗" if f.get("sev") == "error" else "⚠"
                msg = str(f.get("msg") or "")
                fix = str(f.get("fix") or "")
                lines.append(f"{glyph} {msg} — {fix}" if fix else f"{glyph} {msg}")
            body_text = "\n".join(lines)
        else:
            body_text = "Looks complete."
        content: list[Any] = [
            FileEditToolCallContent(
                type="diff", path=path, old_text=None,
                new_text=str(kickoff.get("markdown") or ""),
            ),
            ContentToolCallContent(
                type="content",
                content=TextContentBlock(type="text", text=body_text),
            ),
        ]
        await self._emit(session_id, ToolCallProgress(
            session_update="tool_call_update",
            tool_call_id=kickoff["tool_call_id"],
            content=cast(Any, content),
        ))

    async def _offer_kickoff_draft_approval(
        self, session_id: str, draft_id: str, *, tool_call_id: str, recipe: str
    ) -> None:
        """Ask the user, on the kickoff's own tool card, what to do with it:
        Start run / Save only / Change something / Discard. Awaited inside the
        turn. Dismissing keeps the draft.

        The four options are the kickoff §Agent spec verbatim. The action
        vocabulary differs from the recipe / proposal 3-button set (run vs.
        save vs. change vs. discard) because the kickoff is one document
        rather than a tree — there is no equivalent of "Update" since
        re-drafting the same slug already overwrites the staged file.
        """
        if self._conn is None:
            return
        try:
            response = await self._conn.request_permission(
                session_id=session_id,
                tool_call=ToolCallUpdate(
                    tool_call_id=tool_call_id,
                    kind="edit",
                    title=f"Start {recipe} run?" if recipe else "Start run?",
                    status="pending",
                ),
                options=[
                    PermissionOption(option_id="run", name="Start run", kind="allow_once"),
                    PermissionOption(option_id="save", name="Save only", kind="allow_always"),
                    PermissionOption(option_id="change", name="Change something", kind="reject_once"),
                    PermissionOption(option_id="discard", name="Discard", kind="reject_always"),
                ],
            )
        except Exception:  # noqa: BLE001 — UI is best-effort, the draft survives
            return
        outcome = getattr(response, "outcome", None)
        option_id = getattr(outcome, "option_id", None) if outcome is not None else None
        if option_id in ("run", "save"):
            await self._emit(session_id, ToolCallProgress(
                session_update="tool_call_update",
                tool_call_id=tool_call_id,
                status="completed",
            ))
        if option_id == "run":
            await self._handle_kickoff_run(session_id, draft_id, recipe=recipe)
        elif option_id == "save":
            await self._handle_kickoff_save(session_id, draft_id, recipe=recipe)
        elif option_id == "change":
            await self._handle_kickoff_change(session_id, draft_id)
            await self._emit(session_id, ToolCallProgress(
                session_update="tool_call_update",
                tool_call_id=tool_call_id,
                status="failed",
            ))
        elif option_id == "discard":
            await self._handle_kickoff_discard(session_id, draft_id)
            await self._emit(session_id, ToolCallProgress(
                session_update="tool_call_update",
                tool_call_id=tool_call_id,
                status="failed",
            ))
        else:  # dismissed
            await self._emit(session_id, self._build_refusal_message(
                "Kickoff kept — ask me to start it when you're ready."))
            await self._emit(session_id, ToolCallProgress(
                session_update="tool_call_update",
                tool_call_id=tool_call_id,
                status="failed",
            ))

    async def _handle_kickoff_run(
        self, session_id: str, draft_id: str, *, recipe: str
    ) -> None:
        """``run`` → start the run exactly as ``/run <recipe> <kickoff>`` does.

        Delete the staged draft, then defer to ``_prompt_thread_direct`` —
        it honours the thread's stored recipe + workspace + run-id minting
        + launcher seam + in-thread follow. Per the prior-art lens this
        is the single canonical launch path; reinventing launch logic
        here would bypass worktree creation, env contract, and the
        ``launcher`` / ``_reader`` / ``_stopper`` seams.
        """
        home = self._home_for(session_id)
        staged = home / "kickoff-drafts" / f"{draft_id}.md"
        try:
            markdown = staged.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            markdown = ""
        try:
            staged.unlink()
        except FileNotFoundError:
            pass
        if not recipe or not markdown:
            await self._emit(session_id, self._build_refusal_message(
                "Kickoff is missing its recipe or markdown — draft it again."
            ))
            return
        await self._prompt_thread_direct(session_id, markdown, recipe=recipe)

    async def _handle_kickoff_save(
        self, session_id: str, draft_id: str, *, recipe: str = ""
    ) -> None:
        """``save`` → move the staged draft to ``<home>/kickoffs/<slug>.md``.

        On collision, append ``-2``, ``-3`` … to the destination. The
        canonical line is then surfaced to the user with a file link so
        Zed renders it as a clickable mention. The user runs the saved
        kickoff later via ``/run`` or
        ``mini-ork run <recipe> .mini-ork/kickoffs/<name>.md``.
        """
        from mini_ork.kickoff_lint import slug as _slug

        home = self._home_for(session_id)
        staged = home / "kickoff-drafts" / f"{draft_id}.md"
        target_dir = home / "kickoffs"
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            markdown = staged.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            markdown = ""
        # Use the slug from the markdown content for the saved path —
        # the draft_id is already that slug, but the saved file should
        # reflect the title at save-time, and re-using the same slug keeps
        # a stable identity for the user.
        target_name = draft_id if draft_id else _slug(markdown)
        target = target_dir / f"{target_name}.md"
        if target.exists():
            n = 2
            while True:
                candidate = target_dir / f"{target_name}-{n}.md"
                if not candidate.exists():
                    target = candidate
                    break
                n += 1
        try:
            target.write_text(markdown, encoding="utf-8")
            staged.unlink()
        except OSError as exc:
            await self._emit(session_id, self._build_refusal_message(
                f"`save` failed for `{draft_id}`: {exc}"
            ))
            return
        rel = target.relative_to(home.parent)
        await self._emit(session_id, self._build_refusal_message(
            f"Saved `.mini-ork/kickoffs/{target.name}`. "
            "Run it later with `/run` or "
            f"`mini-ork run {recipe or '<recipe>'} {rel}`."
        ))
        await self._emit_file_links(session_id, [target])

    async def _handle_kickoff_change(
        self, session_id: str, draft_id: str
    ) -> None:
        """``keep`` → keep the staged draft; ask the user what to change."""
        del draft_id
        await self._emit(session_id, self._build_refusal_message(
            "Tell me what to change."
        ))

    async def _handle_kickoff_discard(
        self, session_id: str, draft_id: str
    ) -> None:
        """``discard`` → delete the staged draft and report."""
        home = self._home_for(session_id)
        staged = home / "kickoff-drafts" / f"{draft_id}.md"
        try:
            staged.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            await self._emit(session_id, self._build_refusal_message(
                f"`discard` failed for `{draft_id}`: {exc}"
            ))
            return
        await self._emit(session_id, self._build_refusal_message(
            "Kickoff discarded."
        ))

    async def _offer_recipe_test_run(self, session_id: str, recipe_id: str) -> None:
        """After creating a recipe: offer to run it now on its example kickoff."""
        if self._conn is None:
            return
        tool_call_id = f"recipe-test:{recipe_id}"
        await self._emit(session_id, ToolCallStart(
            session_update="tool_call", tool_call_id=tool_call_id, kind="execute",
            status="pending", title=f"Test {recipe_id} on its example kickoff"))
        try:
            response = await self._conn.request_permission(
                session_id=session_id,
                tool_call=ToolCallUpdate(tool_call_id=tool_call_id, status="pending"),
                options=[
                    PermissionOption(option_id="test", name="Test it now", kind="allow_once"),
                    PermissionOption(option_id="later", name="Not now", kind="reject_once"),
                ],
            )
            outcome = getattr(response, "outcome", None)
            option_id = getattr(outcome, "option_id", None) if outcome is not None else None
        except Exception:  # noqa: BLE001
            option_id = None
        if option_id != "test":
            await self._emit(session_id, ToolCallProgress(
                session_update="tool_call_update", tool_call_id=tool_call_id, status="completed"))
            return
        await self._emit(session_id, ToolCallProgress(
            session_update="tool_call_update", tool_call_id=tool_call_id, status="completed"))
        await self._launch_recipe_test_run(session_id, recipe_id)

    async def _launch_recipe_test_run(self, session_id: str, recipe_id: str) -> None:
        """Run the new recipe on its example kickoff, followed in this thread
        exactly like a direct-mode run (marker, live output, plan, diffs)."""
        rdir = self._home_for(session_id) / "recipes" / recipe_id
        examples = sorted(rdir.glob("examples/*/kickoff.md")) + [rdir / "example-kickoff.md"]
        kickoff = next((e for e in examples if e.is_file()), None)
        if kickoff is None:
            await self._emit(session_id, self._build_refusal_message(
                f"{recipe_id} has no example kickoff to test with — `/run <task>` with Recipe = {recipe_id}."))
            return
        await self._prompt_thread_direct(
            session_id, kickoff.read_text(encoding="utf-8"), recipe=recipe_id)

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
        """Follow a run the orchestrator launched, inside the thread.

        Emits a ``run <run_id> (<recipe>)`` marker, then projects the run like
        any other (lifecycle, live agent output, terminal message) with tool
        call ids prefixed ``"<run_id>:"``. The follower outlives the turn
        until the run ends or the thread is cancelled.
        """
        if not _is_safe_token(child_run_id):
            return
        existing = self._followers.get(child_run_id)
        if existing is not None and not existing.done():
            return  # already following
        cfg = self._thread_config.get(parent_session_id) or {}
        recipe = recipe or str(cfg.get("recipe") or self._recipe)
        # Same project as the thread, so the run's home (db, live files) resolves.
        self._sessions.setdefault(
            child_run_id, self._sessions.get(parent_session_id) or os.getcwd()
        )
        self._routes[child_run_id] = (parent_session_id, f"{child_run_id}:")
        # Z5: track the child run under its parent thread so a later
        # ``/status`` (no run-id) returns the most-recent orchestrator child.
        # Mirrors the append in ``_prompt_thread_direct``.
        runs = self._thread_runs.setdefault(parent_session_id, [])
        if child_run_id not in runs:
            runs.append(child_run_id)
        await self._open_run_marker(parent_session_id, child_run_id, recipe)
        self._followers[child_run_id] = asyncio.create_task(
            self._follow_child(parent_session_id, child_run_id)
        )

    async def _follow_child(self, parent_session_id: str, child_run_id: str) -> None:
        try:
            await self._follow_in_thread(parent_session_id, child_run_id)
        finally:
            self._followers.pop(child_run_id, None)

    async def _open_run_marker(
        self,
        thread_id: str,
        run_id: str,
        recipe: str,
        *,
        worktree_branch: str | None = None,
        adopted: bool = False,
    ) -> None:
        title = f"run {run_id} ({recipe})"
        if worktree_branch:
            # Adopted runs commit onto the existing branch in the user's
            # linked worktree (``this worktree``) — vs. a freshly minted
            # worktree (``worktree``). See kickoff §Where a run works.
            prefix = "this worktree" if adopted else "worktree"
            title = f"{title} · {prefix} {worktree_branch}"
        await self._emit(
            thread_id,
            ToolCallStart(
                session_update="tool_call",
                tool_call_id=f"{run_id}:parent",
                title=title,
                status="in_progress",
                kind="other",
            ),
        )

    async def _follow_in_thread(self, thread_id: str, run_id: str) -> StopReason:
        """Project routed ``run_id`` until it ends, then close its marker.

        A child run's launcher pid belongs to the MCP server, not to us, so
        ``_launches`` holds none for it and only the start timeout applies.

        Zed S5: after the run reaches a terminal status inside an active
        turn (direct mode / ``/run`` / orchestrator child), inspect the new
        task state. A "ready to review" state means the run ended on a
        workspace branch with commits ahead — offer Merge / Discard /
        Keep buttons on the run's own ``<run_id>:parent`` tool card. The
        marker stays ``in_progress`` until the user picks; ``merge`` /
        ``discard`` flips it to ``completed`` and re-emits the title from
        the new state.
        """
        stop = await self._await_terminal(run_id)
        if stop == "cancelled":
            return stop  # the run may still be going; leave its marker open
        ok = stop == "end_turn" and self._run_status.get(run_id) == "published"
        if ok and await self._deliver_to_zed(thread_id, run_id):
            await self._emit(thread_id, ToolCallProgress(
                session_update="tool_call_update",
                tool_call_id=f"{run_id}:parent",
                status="completed",
            ))
            await self._reemit_title_after_review(run_id)
            return stop
        ws_ready = await self._offer_run_review(thread_id, run_id)
        if not ws_ready:
            await self._emit(
                thread_id,
                ToolCallProgress(
                    session_update="tool_call_update",
                    tool_call_id=f"{run_id}:parent",
                    status="completed" if ok else "failed",
                ),
            )
        return stop

    async def _deliver_to_zed(self, thread_id: str, run_id: str) -> bool:
        """Hand a finished run's change to Zed as agent edits.

        Each changed file is written into the thread's project through the
        client (ACP ``fs/write_text_file``). Zed applies it as an agent edit
        and records it in the thread's action log, so the change shows up
        where Zed shows its own agents' work: the Threads Sidebar row's +/−,
        the changed-files bar above the message box, Review Changes (Keep /
        Reject per hunk), the Git panel. The run's worktree is then removed.

        All or nothing: a file the user changed during the run, a binary file
        or a deletion (ACP cannot delete) leaves the project untouched and
        returns False, so the Merge / Discard buttons take over.
        """
        if self._conn is None or not self.client_supports("fs.write"):
            return False
        if run_id in self._skip_review:
            return False
        from mini_ork import workspaces as _workspaces

        ws = _workspaces.load(self._home_for(run_id), run_id)
        if ws is None or ws.adopted:
            return False
        thread_cwd = Path(self._sessions.get(thread_id) or ws.project)
        rc, top, _ = _workspaces._git(["rev-parse", "--show-toplevel"], thread_cwd)
        project = Path(top.strip()) if rc == 0 and top.strip() else thread_cwd
        changes = _workspaces.changed_paths(ws)
        if not changes:
            return False
        writes: list[tuple[Path, str]] = []
        blocked: list[str] = []
        for status, rel in changes:
            if status == "D":
                blocked.append(f"`{rel}` (the run deleted it)")
                continue
            try:
                new_text = (ws.path / rel).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                blocked.append(f"`{rel}` (not a text file)")
                continue
            target = project / rel
            try:
                current = target.read_text(encoding="utf-8") if target.is_file() else None
            except (OSError, UnicodeDecodeError):
                current = ""
            if current != _workspaces.base_text(ws, rel):
                blocked.append(f"`{rel}` (you changed it while the run worked)")
                continue
            writes.append((target, new_text))
        if blocked:
            await self._emit(thread_id, self._build_refusal_message(
                "\n\nNot handed to your project, so nothing in it changed: "
                + ", ".join(blocked) + ". Use the buttons below instead."))
            return False
        for target, new_text in writes:
            try:
                await self._conn.write_text_file(
                    session_id=thread_id, path=str(target), content=new_text)
            except Exception as exc:  # noqa: BLE001 — report and keep the worktree
                await self._emit(thread_id, self._build_refusal_message(
                    f"\n\nZed did not accept `{target}`: {exc}. The run's worktree is kept: "
                    f"/merge {run_id} or /discard {run_id}."))
                return False
        _workspaces.discard(ws)
        added = sum(1 for s, _ in changes if s == "A")
        n = len(writes)
        await self._emit(thread_id, self._build_refusal_message(
            f"\n\nThe change is in your project now: {n} file{'s' if n != 1 else ''}"
            + (f" ({added} new)" if added else "")
            + ". Review it like any agent edit — **Review Changes**, or the changed-files"
            " bar above the message box — and Keep or Reject each change.\n\n"))
        return True

    async def _offer_run_review(self, thread_id: str, run_id: str, *, force: bool = False) -> bool:
        """Offer Merge / Discard / Keep buttons on the run's marker when the
        run ended on a workspace with commits ahead.

        Returns ``True`` when the buttons were offered OR the one-time
        message fired (the marker stays ``in_progress`` until the user
        picks or runs ``/merge`` / ``/discard``); ``False`` when the run
        ended cleanly with no worktree to clean up — the caller closes
        the marker immediately.

        An "active turn" means either a direct-mode prompt whose caller
        is still awaiting (recorded in ``_direct_runs``) or an orchestrator
        turn that has not yet finished (recorded in ``_orchestrator_tasks``).
        Inactive → no buttons, just the one-time message. ``force=True``
        overrides the active-turn check; it is used by the orchestrator
        turn-end pass (``_offer_thread_review``) to surface buttons for
        runs that became ready-to-review during the turn.
        """
        if run_id in self._ready_to_review_emitted:
            return True
        # Zed S7a: race runs surface their decision on the parent
        # permission card, not on a per-run S5 marker. Skipping here lets
        # ``_follow_in_thread`` close the marker cleanly.
        if run_id in self._skip_review:
            return False
        run_dir = self._home_for(run_id) / "runs" / run_id
        if not run_dir.is_dir():
            return False
        snap = (self._reader or self._read_snapshot)(run_id) or {}
        ts = _task_state.task_state(run_dir, snap)
        if ts.state != "needs_you" or not ts.detail.startswith("Ready to review:"):
            return False
        active_turn = force or (
            self._direct_runs.get(thread_id) == run_id
            or self._orchestrator_tasks.get(thread_id) is not None
        )
        if not active_turn or self._conn is None:
            self._ready_to_review_emitted.add(run_id)
            await self._emit_ready_to_review_once(thread_id, run_id, ts)
            return True
        from mini_ork import workspaces as _workspaces

        ws = _workspaces.load(self._home_for(run_id), run_id)
        base_branch = ws.base_branch if ws is not None else ""
        try:
            response = await self._conn.request_permission(
                session_id=thread_id,
                tool_call=ToolCallUpdate(
                    tool_call_id=f"{run_id}:parent",
                    kind="edit",
                    title=f"Review run {run_id} (+{ts.added} −{ts.removed})",
                    status="pending",
                ),
                options=[
                    PermissionOption(
                        option_id="merge",
                        name=f"Merge into {base_branch}" if base_branch else "Merge",
                        kind="allow_once",
                    ),
                    PermissionOption(option_id="discard", name="Discard changes", kind="reject_always"),
                    PermissionOption(option_id="keep", name="Keep for later", kind="reject_once"),
                ],
            )
        except Exception:  # noqa: BLE001 — UI is best-effort, marker stays open
            return True
        outcome = getattr(response, "outcome", None)
        option_id = getattr(outcome, "option_id", None) if outcome is not None else None
        self._ready_to_review_emitted.add(run_id)
        await self._handle_review_decision(thread_id, run_id, option_id, ts)
        return True

    async def _handle_review_decision(
        self,
        thread_id: str,
        run_id: str,
        option_id: str | None,
        ts: _TaskState,
    ) -> None:
        """Apply the user's pick: merge → fast-forward into the project;
        discard → remove the worktree + branch; keep / dismissed → marker
        stays ``in_progress`` so the user can decide later."""
        from mini_ork import workspaces as _workspaces

        home = self._home_for(run_id)
        ws = _workspaces.load(home, run_id)
        if option_id == "merge" and ws is not None:
            # The task's own title is what the user wants in git history.
            base = self._thread_titles.get(thread_id) or self._run_base_titles.get(run_id) or ""
            message = f"{base} (mini-ork {run_id})" if base else f"mini-ork run {run_id}"
            try:
                result = _workspaces.merge(ws, message=message)
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "error": str(exc)}
            if result.get("ok"):
                merged_sha = result.get("merged") or ""
                short = (str(merged_sha)[:7]) if merged_sha else ""
                mode = result.get("mode") or "merge"
                wording = (
                    "fast-forward" if mode == "fast-forward" else "merge commit"
                )
                await self._emit(
                    thread_id,
                    self._build_refusal_message(
                        f"Merged into {ws.base_branch} ({wording} {short})."
                    ),
                )
                self._ready_to_review_emitted.discard(run_id)
                await self._close_run_marker_completed(thread_id, run_id)
                await self._reemit_title_after_review(run_id)
                return
            err = (result.get("error") if isinstance(result, dict) else None) or "unknown error"
            await self._emit(
                thread_id,
                self._build_refusal_message(
                    f"Merge refused: {err}. The worktree is kept — fix it, then /merge {run_id}."
                ),
            )
            return
        if option_id == "discard" and ws is not None:
            try:
                _workspaces.discard(ws)
            except Exception as exc:  # noqa: BLE001
                await self._emit(
                    thread_id,
                    self._build_refusal_message(
                        f"Discard failed: {exc}. The worktree is kept — /discard {run_id} to retry."
                    ),
                )
                return
            await self._emit(
                thread_id,
                self._build_refusal_message(
                    f"Discarded {run_id} — its worktree and branch are gone."
                ),
            )
            self._ready_to_review_emitted.discard(run_id)
            await self._close_run_marker_completed(thread_id, run_id)
            await self._reemit_title_after_review(run_id)
            return
        # keep / dismissed / no workspace: marker stays in_progress; one-line message.
        branch = (
            ts.detail.split(" on ", 1)[-1].split(" — ", 1)[0] if " on " in ts.detail else ""
        )
        where = f" on {branch}" if branch else ""
        await self._emit(
            thread_id,
            self._build_refusal_message(
                f"Kept{where} — /merge {run_id} or /discard {run_id} when you're ready."
            ),
        )

    async def _offer_thread_review(self, thread_id: str) -> None:
        """Orchestrator turn-end pass: walk the runs this thread followed
        during the turn, newest-first, and offer buttons for any that are
        ready-to-review (S5).

        The ``_offer_run_review`` call uses ``force=True`` so the
        active-turn guard does not flip into the one-time-message path;
        the orchestrator task is already popped by this point, but the
        user's prompt turn is still open until we return ``end_turn``.
        """
        runs = self._thread_runs.get(thread_id) or []
        for run_id in reversed(runs):
            await self._offer_run_review(thread_id, run_id, force=True)

    async def _emit_ready_to_review_once(
        self, thread_id: str, run_id: str, ts: _TaskState | None = None
    ) -> None:
        """One-time message for a child run that ended after the orchestrator's
        turn ended (no active UI to host the buttons). The title already shows
        ✋ via the cheap ``run_mark`` extension; the message tells the user how
        to resolve it via slash commands.
        """
        if run_id in self._ready_to_review_emitted:
            return
        if ts is None:
            run_dir = self._home_for(run_id) / "runs" / run_id
            ts = _task_state.task_state(
                run_dir,
                (self._reader or self._read_snapshot)(run_id) or {},
            )
        branch = ""
        if " on " in ts.detail:
            branch = ts.detail.split(" on ", 1)[-1].split(" — ", 1)[0]
        where = f" on {branch}" if branch else ""
        self._ready_to_review_emitted.add(run_id)
        await self._emit(
            thread_id,
            self._build_refusal_message(
                f"Run {run_id} is ready to review (+{ts.added} −{ts.removed}{where}): "
                f"/merge {run_id} or /discard {run_id}."
            ),
        )

    async def _close_run_marker_completed(self, thread_id: str, run_id: str) -> None:
        await self._emit(
            thread_id,
            ToolCallProgress(
                session_update="tool_call_update",
                tool_call_id=f"{run_id}:parent",
                status="completed",
            ),
        )

    async def _reemit_title_after_review(self, run_id: str) -> None:
        """Re-emit the title from the post-decision task state.

        A merge / discard removes the workspace record, so the next
        ``task_state`` projection lands back on the plain ``done`` /
        ``failed`` branch — the user sees ``✓`` / ``✗`` instead of ``✋``.
        The dedup key in ``_last_title_sent[dest]`` is the same string
        the projection wrote before, so the new title is different and
        fires; the entry is rewritten here.
        """
        run_dir = self._home_for(run_id) / "runs" / run_id
        snap = (self._reader or self._read_snapshot)(run_id) or {}
        new_ts = _task_state.task_state(run_dir, snap)
        route = self._routes.get(run_id)
        dest = route[0] if route is not None else run_id
        if dest.startswith("orch-"):
            base = self._thread_titles.get(dest) or "mini-ork thread"
        else:
            base = self._run_base_titles.get(dest) or dest
        new_title = _task_state.title_with_state(base, new_ts)
        if self._last_title_sent.get(dest) == new_title:
            return
        self._last_title_sent[dest] = new_title
        await self._emit(
            run_id,
            SessionInfoUpdate(
                session_update="session_info_update",
                title=new_title,
            ),
        )
        if dest.startswith("orch-"):
            self._record(
                dest,
                {"type": "title", "title": new_title},
            )

    async def _default_orchestrator_turn(
        self,
        *,
        lane: str,
        prompt: str,
        cwd: Path,
        home: Path,
        resume: str | None,
        on_event: Callable[[dict[str, Any]], Awaitable[None]],
        workspace_mode: str | None = None,
    ) -> Any:
        """Default ``orchestrator_turn`` — delegates to the orchestrator harness.

        Deferred so the orchestrator package's heavy imports (dispatch layer,
        provider registry) only land when an orchestrator turn actually runs,
        not at agent construction time. The CLI process spawns a real
        ``claude`` subprocess here; tests inject a fake.

        ``workspace_mode`` (Zed S4) is forwarded to the spawned MCP server
        subprocess via ``MO_WORKSPACE_MODE`` so ``start_run`` inside the
        orchestrator's own tool calls lands on the same workspace choice as
        the parent thread. ``None`` falls through to the harness default.
        """
        from mini_ork.acp_orchestrator.harness import run_turn

        extra_mcp_env: dict[str, str] = {}
        if workspace_mode:
            extra_mcp_env["MO_WORKSPACE_MODE"] = workspace_mode
        if cwd:
            # start_run works from the thread's own checkout; the MCP server
            # only knows the home, i.e. the main checkout.
            extra_mcp_env["MO_THREAD_CWD"] = str(cwd)
        if self.client_supports("fs.write"):
            # The agent hands finished runs to the client as agent edits, so
            # runs must not edit the thread's checkout in place.
            extra_mcp_env["MO_DELIVER_VIA_CLIENT"] = "1"
        return await run_turn(
            lane=lane,
            prompt=prompt,
            cwd=cwd,
            home=home,
            resume=resume,
            on_event=on_event,
            extra_mcp_env=extra_mcp_env,
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
            direct = self._direct_runs.get(session_id)
            if direct:
                # A direct run IS the turn: stop it like a run session.
                await self.cancel(direct)
            race_runs = self._race_runs.get(session_id) or []
            if race_runs:
                # A race's runs are the turn too: stop every contestant, at
                # once (each waits out its own grace before a hard kill).
                await asyncio.gather(*(self.cancel(r) for r in race_runs))
            for run_id, (thread_id, _prefix) in list(self._routes.items()):
                if thread_id != session_id or run_id == direct:
                    continue
                child_task = self._followers.pop(run_id, None)
                if child_task is not None and not child_task.done():
                    child_task.cancel()
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

    def _build_terminal_message(
        self, status: str, run_id: str | None = None
    ) -> AgentMessageChunk:
        run = f"run {run_id}" if run_id else "run"
        return AgentMessageChunk(
            session_update="agent_message_chunk",
            content=TextContentBlock(type="text", text=f"mini-ork {run} finished: {status}\n\n"),
        )

    def _thread_usage_update(self, thread_id: str) -> UsageUpdate:
        """The thread's cumulative cost: orchestrator turns + every run it followed."""
        total = sum(self._thread_costs.get(thread_id, {}).values())
        return UsageUpdate(
            session_update="usage_update",
            used=0,
            size=DEFAULT_CONTEXT_SIZE,
            cost=Cost(amount=total, currency="USD"),
        )

    def _build_refusal_message(self, text: str) -> AgentMessageChunk:
        """Same shape as ``_build_terminal_message``; emitted when the turn
        ends in ``refusal`` (launcher died or start timeout exceeded).
        """
        return AgentMessageChunk(
            session_update="agent_message_chunk",
            content=TextContentBlock(type="text", text=text),
        )

    # ── implementer diff projection (Z4) ──────────────────────────────────────

    @staticmethod
    def _implementer_node_id(snapshot: dict[str, Any]) -> str:
        """Return the implementer node id from lifecycle events, or the literal
        ``"implementer"`` when the snapshot has none.

        The recipe convention is one ``implementer`` per recipe run, so the
        literal fallback matches the kickoff's "falling back to
        ``implementer``" contract — even an empty snapshot (status set, no
        events yet) still produces a usable id.
        """
        for ev in snapshot.get("events") or []:
            if ev.get("event_type") not in ("node_start", "node_end"):
                continue
            payload = ev.get("payload_json") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (json.JSONDecodeError, TypeError):
                    payload = {}
            node_type = str(payload.get("node_type") or ev.get("node_type") or "")
            if node_type == "implementer":
                return str(payload.get("node_id") or ev.get("node_id") or "implementer")
        return "implementer"

    @staticmethod
    def _build_diff_progress(
        node_id: str, diffs_list: list[dict[str, Any]]
    ) -> ToolCallProgress:
        """Wrap ``diffs_list`` as the content of one ``ToolCallProgress``.

        Empty input still yields a valid ``ToolCallProgress`` with an empty
        content list — the caller checks ``diffs_list`` before deciding
        whether to emit it (an empty update on the wire is wasted bandwidth
        and confusing to clients).
        """
        content: list[Any] = [
            FileEditToolCallContent(
                type="diff",
                path=str(entry["path"]),
                old_text=entry.get("old_text"),
                new_text=str(entry.get("new_text", "")),
            )
            for entry in diffs_list
        ]
        return ToolCallProgress(
            session_update="tool_call_update",
            tool_call_id=node_id,
            status="completed",
            content=cast(Any, content),
        )

    async def _emit_diff_update_for_run(
        self, session_id: str, node_id: str
    ) -> None:
        """Live path — fire one diff update the first time an implementer
        ``node_end`` for ``(session_id, node_id)`` is seen.

        Reads the run dir, computes diffs (which also writes the cache), and
        emits one ``ToolCallProgress`` through ``_emit`` — that means a run
        followed inside a thread gets the ``"<run_id>:"`` prefix automatically.
        A subsequent poll that re-projects the same ``node_end`` is a no-op
        via ``_diff_emitted``.
        """
        if (session_id, node_id) in self._diff_emitted:
            return
        run_dir = self._home_for(session_id) / "runs" / session_id
        if not run_dir.is_dir():
            return
        diffs_list = _diffs.run_diffs(run_dir)
        if not diffs_list:
            return
        self._diff_emitted.add((session_id, node_id))
        await self._emit(session_id, self._build_diff_progress(node_id, diffs_list))

    async def _emit_diff_update_for_loaded_run(
        self, session_id: str, snapshot: dict[str, Any]
    ) -> None:
        """Load path — fire one diff update (cache or computed) after replay.

        When the run's status is ``rolled_back``, emit a single agent
        message instead. When the cache is absent, prefix a "showing the
        files as they are now — they may have changed since the run" note
        so a future editor is not silently misled. The diffs attach to the
        implementer node id resolved from the lifecycle events
        (fallback ``"implementer"``). Dedup keyed on ``(session_id,
        node_id)`` mirrors the live path.
        """
        status = snapshot.get("status")
        node_id = self._implementer_node_id(snapshot)
        if (session_id, node_id) in self._diff_emitted:
            return
        if status == "rolled_back":
            self._diff_emitted.add((session_id, node_id))
            await self._emit(
                session_id,
                AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(
                        type="text",
                        text=f"Run {session_id}: its changes were rolled back; nothing to review.",
                    ),
                ),
            )
            return
        run_dir = self._home_for(session_id) / "runs" / session_id
        if not run_dir.is_dir():
            return
        diffs_list, from_cache = _diffs.cached_or_computed(run_dir)
        if not diffs_list:
            return
        self._diff_emitted.add((session_id, node_id))
        if not from_cache:
            await self._emit(
                session_id,
                AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(
                        type="text",
                        text=(
                            f"\n\nRun {session_id}: showing the files as they are now — "
                            "they may have changed since the run."
                        ),
                    ),
                ),
            )
        await self._emit(session_id, self._build_diff_progress(node_id, diffs_list))

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
        origin = session_id
        route = self._routes.get(session_id)
        if route is not None:
            # A run followed inside a thread: address the thread, keep the
            # run's tool calls apart from the orchestrator's, fold its cost in.
            thread_id, prefix = route
            if isinstance(update, UsageUpdate):
                amount = float(update.cost.amount) if update.cost else 0.0
                self._thread_costs.setdefault(thread_id, {})[session_id] = amount
                update = self._thread_usage_update(thread_id)
                if self._usage_sent.get(thread_id) == update.cost.amount:
                    return
                self._usage_sent[thread_id] = update.cost.amount
            elif getattr(update, "tool_call_id", None):
                update = update.model_copy(
                    update={"tool_call_id": f"{prefix}{update.tool_call_id}"}
                )
            session_id = thread_id
        # Z9c-2: persist what the thread itself said. A routed run's internals
        # are not recorded (load re-projects them from the run's own data);
        # its usage is, as the thread's cost snapshot. Replays never record.
        if (
            session_id in self._thread_sessions
            and session_id not in self._replaying
            and (origin == session_id or isinstance(update, UsageUpdate))
        ):
            self._record_thread_update(session_id, update)
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
        if status is not None:
            self._run_status[session_id] = str(status)
        if status in TERMINAL_STATUSES:
            routed = session_id if session_id in self._routes else None
            updates.append(self._build_terminal_message(status, run_id=routed))
        for update in updates:
            await self._emit(session_id, update)
        # Z6: render the run's DAG as a checklist above the thread. One
        # ``AgentPlanUpdate`` per destination per content change; an older
        # routed run does not overwrite the thread's latest plan.
        await self._emit_plan_update(session_id, snapshot)
        # Zed S1: render the run's task state as a thread-list title
        # (working / needs_you / done / failed). Deduped per destination
        # and gated on the latest-run rule so a thread with two children
        # only shows the latest's mark.
        await self._emit_title_state_update(session_id, snapshot)
        if not live:
            # A replay (load) shows the run's recorded diff — see
            # _emit_diff_update_for_loaded_run; recomputing here would diff
            # today's files and overwrite the cache with them.
            return
        # Z4: surface the implementer's file edits under the implementer's
        # tool call so Zed's Review Changes picks them up. Fires once per
        # (run, node) on the implementer's ``node_end``; subsequent polls
        # of the same snapshot are deduped by ``_diff_emitted``. The diff
        # computation also writes the cache, so a later ``load_session``
        # replays from disk without re-invoking git.
        for ev in new_events:
            if ev.get("event_type") != "node_end":
                continue
            payload = ev.get("payload_json") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (json.JSONDecodeError, TypeError):
                    payload = {}
            node_type = str(payload.get("node_type") or ev.get("node_type") or "")
            node_id = str(payload.get("node_id") or ev.get("node_id") or "implementer")
            if node_type == "implementer" or node_id == "implementer":
                await self._emit_diff_update_for_run(session_id, node_id)

    async def _emit_plan_update(
        self, session_id: str, snapshot: dict[str, Any]
    ) -> None:
        """Project the run's DAG into an ``AgentPlanUpdate`` and emit (Z6).

        Three gates before an emit fires:

        1. ``plan_entries`` returned a non-empty list (a missing or unparseable
           ``plan.json`` is the dominant miss — kickoff §``plan.py``).
        2. The run is either standalone or the thread's **latest** run
           (``self._thread_runs[thread][-1]``). An older routed run still
           being followed must NOT overwrite the thread's plan (Z5).
        3. The canonicalised tuple of ``(content, status, priority)`` for
           the destination differs from the last value stored in
           ``_plan_emitted[dest]``.

        The emit flows through ``_emit`` so the routing redirect plus the
        per-thread ``AgentPlanUpdate`` JSONL record`` land on the same wire as
        every other update family.
        """
        run_dir = self._home_for(session_id) / "runs" / session_id
        entries = _plan.plan_entries(run_dir, snapshot.get("events") or [])
        if not entries:
            return
        route = self._routes.get(session_id)
        if route is not None:
            thread_id = route[0]
            latest = self._thread_runs.get(thread_id)
            if latest and latest[-1] != session_id:
                # Older run still being followed: leave the thread's plan
                # to the latest run.
                return
            dest = thread_id
        else:
            dest = session_id
        canonical: tuple[tuple[str, str, str], ...] = tuple(
            (str(e["content"]), str(e["status"]), str(e["priority"]))
            for e in entries
        )
        if self._plan_emitted.get(dest) == canonical:
            return
        self._plan_emitted[dest] = canonical
        await self._emit(
            session_id,
            AgentPlanUpdate(
                session_update="plan",
                entries=[PlanEntry(**e) for e in entries],
            ),
        )

    async def _emit_title_state_update(
        self, session_id: str, snapshot: dict[str, Any]
    ) -> None:
        """Project the run's task state into a ``SessionInfoUpdate`` title (Zed S1).

        Three gates before an emit fires:

        1. Not currently replaying — ``_load_thread_session`` re-projects
           every record and would record a title per state change if not
           guarded.
        2. The run is either standalone (a non-routed run session gets
           the title on its own session) or the thread's **latest** run
           (an older routed run must NOT overwrite the latest's title).
        3. The new title differs from the last one sent for the
           destination, so a 2nd poll that lands the same state is a
           no-op emit.

        A ``needs_you`` transition also emits ONE agent message with
        the detail (cost-pause explanation, blocked question), and
        records a ``{"type": "title", ...}`` record so a later
        ``load_session`` replays the same title.
        """
        if session_id in self._replaying:
            return
        run_dir = self._home_for(session_id) / "runs" / session_id
        route = self._routes.get(session_id)
        if route is not None:
            thread_id = route[0]
            # A thread-load replay sets ``_replaying[thread_id]``; suppress
            # the title emit so a re-opened thread does not re-emit a title
            # on every snapshot it walks (the persisted title records are
            # already the source of truth, surfaced via ``list_threads``).
            if thread_id in self._replaying:
                return
            latest = self._thread_runs.get(thread_id)
            if latest and latest[-1] != session_id:
                return
            dest = thread_id
        else:
            dest = session_id
        # The base title for a thread is its first-prompt title (set
        # when ``_prompt_thread`` first emits); for a run session, the
        # kickoff-derived title from ``list_runs`` (set lazily here).
        if dest.startswith("orch-"):
            base = self._thread_titles.get(dest) or "mini-ork thread"
        else:
            base = self._run_base_titles.get(dest) or dest  # no kickoff title: the run id
        ts = _task_state.task_state(run_dir, snapshot)
        new_title = _task_state.title_with_state(base, ts)
        if self._last_title_sent.get(dest) == new_title:
            return
        self._last_title_sent[dest] = new_title
        await self._emit(
            session_id,
            SessionInfoUpdate(
                session_update="session_info_update",
                title=new_title,
            ),
        )
        # Persist the title record directly: ``_record_thread_update``
        # skips ``SessionInfoUpdate`` (line 2214).
        if dest.startswith("orch-"):
            self._record(
                dest,
                {"type": "title", "title": new_title},
            )
        # ``needs_you`` detail fires once per (destination, detail) —
        # a state flip back to working clears the entry so a later
        # flip into needs_you emits again.
        if ts.state == "needs_you" and not (
            ts.detail.startswith("Ready to review:") and self.client_supports("fs.write")
        ):
            if self._last_needs_you_sent.get(dest) != ts.detail:
                self._last_needs_you_sent[dest] = ts.detail
                await self._emit(
                    session_id,
                    AgentMessageChunk(
                        session_update="agent_message_chunk",
                        content=TextContentBlock(
                            type="text",
                            text=ts.detail,
                        ),
                    ),
                )
        else:
            self._last_needs_you_sent.pop(dest, None)

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

    # ── slash-command wiring (Z5) ─────────────────────────────────────────────

    async def _emit_available_commands(self, session_id: str) -> None:
        """Announce the slash-command table to the client for ``session_id``.

        Called after ``session/new`` and after ``session/load`` (run + thread).
        Errors are swallowed: a missing ACP SDK or a connection that has not
        been bound yet must not break session creation / load.
        """
        try:
            await self._emit(
                session_id,
                AvailableCommandsUpdate(
                    session_update="available_commands_update",
                    available_commands=_commands.COMMANDS,
                ),
            )
        except Exception:  # noqa: BLE001 — announcement is best-effort
            pass

    async def _dispatch_slash(
        self, session_id: str, text: str
    ) -> PromptResponse | None:
        """Route a leading-``/`` prompt to a slash handler.

        Returns ``None`` when the prompt is not a slash command (so the
        caller falls through to the normal session-type routing). Returns
        ``PromptResponse(stop_reason="end_turn")`` after dispatching a
        known non-``run`` command or replying to an unknown one. ``/run``
        is intentionally NOT handled here — it stays in the existing
        ``_strip_slash_run`` flow so the recipe picker stays canonical.
        """
        stripped = text.strip()
        if not stripped.startswith("/"):
            return None
        # ``/word arg1 arg2`` — split on the first whitespace after ``/``.
        body = stripped[1:]
        if not body:
            return None
        # S3b-2: match the LONGEST handler key whose token boundary aligns
        # with the body prefix, so ``/recipe new foo`` lands on the
        # ``recipe new`` handler with ``arg="foo"`` instead of falling through
        # to ``/recipe foo``. The bare ``/recipe <id>`` case still works
        # because ``recipe`` is the shortest matching key.
        name = ""
        arg = ""
        for key in sorted(_commands.HANDLERS, key=len, reverse=True):
            if body == key:
                name = key
                arg = ""
                break
            if body.startswith(key + " "):
                name = key
                arg = body[len(key) + 1 :].strip()
                break
        if not name:
            # Fallback: first whitespace-delimited word + the rest as arg.
            parts = body.split(None, 1)
            name = parts[0]
            arg = parts[1] if len(parts) > 1 else ""
        # ``/run`` is preserved by the existing carve-out (threads) and by
        # the run-session launcher (the run id IS the session id); the
        # command table advertises it but it is never dispatched here.
        if name == "run":
            return None
        if name == "race":
            # ``/race`` is handled by ``_prompt_thread_race`` further down
            # the thread prompt path — but a non-thread session must be
            # refused inline (mirrors the thread-only gate on ``_race``);
            # returning None here would let the launcher see ``/race …``
            # as a kickoff body. Fall through ONLY for thread sessions.
            if session_id not in self._thread_sessions:
                await self._emit(
                    session_id,
                    AgentMessageChunk(
                        session_update="agent_message_chunk",
                        content=TextContentBlock(
                            type="text",
                            text="Races start in a mini-ork thread.",
                        ),
                    ),
                )
                return PromptResponse(stop_reason="end_turn")
            return None
        if session_id in self._thread_sessions:
            # Keep the command with its reply in the thread's log, so a
            # reopened thread replays both (the reply is recorded by _emit).
            self._record(session_id, {"type": "user", "text": stripped})
        if name not in _commands.HANDLERS:
            await self._emit(
                session_id,
                AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(
                        type="text",
                        text=f"Unknown command /{name} — /help lists commands.",
                    ),
                ),
            )
            return PromptResponse(stop_reason="end_turn")
        reply = await _commands.handle(self, session_id, name, arg)
        # S3b-2: ``_RewriteToOrchestrate`` hands this command to the
        # orchestrator in this same turn: stash the instruction, say so in
        # one line, and return None so ``prompt`` falls through to
        # ``_prompt_thread``, which runs the orchestrator with it. The
        # approval buttons appear only once a draft actually exists.
        if isinstance(reply, _commands._OfferCopy):
            await self._offer_recipe_copy(session_id, reply.recipe_id)
            return PromptResponse(stop_reason="end_turn")
        if isinstance(reply, _commands._RewriteToOrchestrate):
            self._thread_rewrites[session_id] = reply.intent_text
            # ``bridge`` is the per-flow one-liner that previews what the
            # user is about to see (``create`` buttons + proposal card vs.
            # diffs + draft card). Defaults to the recipe-flow text when
            # the handler did not set one, so existing recipe flows keep
            # their wording (S3b-2).
            bridge_text = reply.bridge or (
                "Handing this to the orchestrator. When it has a "
                "draft you'll see its files as diffs and the "
                "buttons to create it."
            )
            await self._emit(
                session_id,
                AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(type="text", text=bridge_text),
                ),
            )
            return None
        # ``reply`` is either ``str`` (text-only handler) or ``CommandReply``
        # (markdown + file paths). Walk the envelope and emit one text chunk
        # first, then one ``ResourceContentBlock(type="resource_link")`` per
        # path — Zed renders those as clickable file mentions. ``path.resolve()
        # `` collapses symlinks so a macOS ``/private/var/...`` resolves to the
        # volume form ``file:///Volumes/...``.
        if isinstance(reply, _commands.CommandReply):
            markdown = reply.text
            links = reply.links
        else:
            markdown = reply
            links = []
        await self._emit(
            session_id,
            AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=markdown),
            ),
        )
        await self._emit_file_links(session_id, links)
        return PromptResponse(stop_reason="end_turn")

    async def _emit_file_links(self, session_id: str, paths: list[Path]) -> None:
        """One clickable file mention per path (ResourceLink); inside a recipe
        the name is the path within it (prompts/editor.md)."""
        for path in paths:
            try:
                resolved = Path(path).resolve()
                uri = resolved.as_uri()
            except (OSError, ValueError):
                continue
            parts = resolved.parts
            if "recipes" in parts and parts.index("recipes") + 2 < len(parts):
                name = "/".join(parts[parts.index("recipes") + 2:])
            else:
                name = resolved.name
            await self._emit(session_id, AgentMessageChunk(
                session_update="agent_message_chunk",
                content=ResourceContentBlock(type="resource_link", uri=uri, name=name)))

    async def _offer_recipe_copy(self, session_id: str, recipe_id: str) -> None:
        """An engine recipe is edited as a project copy: ask, then copy it into
        .mini-ork/recipes/<id> (overriding the engine's) and link its files."""
        from mini_ork.recipe_author import copy_to_project

        tool_call_id = f"recipe-copy:{recipe_id}"
        await self._emit(session_id, ToolCallStart(
            session_update="tool_call", tool_call_id=tool_call_id, kind="edit", status="pending",
            title=f"Copy recipe {recipe_id} into this project to edit it"))
        option_id = None
        if self._conn is not None:
            try:
                response = await self._conn.request_permission(
                    session_id=session_id,
                    tool_call=ToolCallUpdate(tool_call_id=tool_call_id, status="pending"),
                    options=[
                        PermissionOption(option_id="copy", name="Copy into this project", kind="allow_once"),
                        PermissionOption(option_id="cancel", name="Cancel", kind="reject_once"),
                    ],
                )
                outcome = getattr(response, "outcome", None)
                option_id = getattr(outcome, "option_id", None) if outcome is not None else None
            except Exception:  # noqa: BLE001 — treat as cancelled
                option_id = None
        if option_id != "copy":
            await self._emit(session_id, ToolCallProgress(
                session_update="tool_call_update", tool_call_id=tool_call_id, status="failed"))
            return
        home = self._home_for(session_id)
        result = copy_to_project(home, recipe_id)
        if not result.get("ok"):
            await self._emit(session_id, ToolCallProgress(
                session_update="tool_call_update", tool_call_id=tool_call_id, status="failed"))
            await self._emit(session_id, self._build_refusal_message(
                f"Could not copy {recipe_id}: {result.get('error')}"))
            return
        await self._emit(session_id, ToolCallProgress(
            session_update="tool_call_update", tool_call_id=tool_call_id, status="completed"))
        target = home / "recipes" / recipe_id
        await self._emit(session_id, self._build_refusal_message(
            f"Copied to `.mini-ork/recipes/{recipe_id}` — edit its files below; the project "
            "copy overrides the engine's."))
        await self._emit_file_links(session_id, [target / rel for rel in result.get("files") or []])

    # ── thread-store helpers (Z9c-2) ─────────────────────────────────────────

    def _thread_store(self, thread_id: str) -> ThreadStore:
        """The store under the thread's bound home (built per call: no I/O)."""
        return ThreadStore(self._home_for(thread_id))

    def _record(self, thread_id: str, record: dict[str, Any]) -> None:
        """Append ``record`` to ``thread_id``'s JSONL; swallows I/O errors."""
        try:
            self._thread_store(thread_id).append(thread_id, record)
        except (OSError, ValueError):
            # ``ThreadStore.append`` already swallows I/O errors; this is
            # a defence-in-depth catch so a misbehaving subclass can never
            # break a live thread.
            pass

    def _record_thread_update(self, thread_id: str, update: Any) -> None:
        """Persist one ACP update as a ``{"type": "update", ...}`` record.

        ``UsageUpdate`` is recorded as a ``costs`` snapshot (the thread's
        cumulative cost map), never as an ``update`` — the wire format
        ``cost.amount`` is per-update and would not round-trip cleanly.
        ``SessionInfoUpdate`` is never recorded (it's an artefact of the
        first-prompt title push; the recorded ``meta``/``config``/``user``
        already carry the same information). When a child run's tool call
        lands via ``_emit`` the original id is a routed run id
        (``run-...``), NOT in ``_thread_sessions``, so the outer
        ``_thread_sessions`` guard already skips recording routed updates.
        """
        if isinstance(update, UsageUpdate):
            costs = self._thread_costs.get(thread_id, {})
            self._record(
                thread_id,
                {"type": "costs", "costs": {str(k): float(v) for k, v in costs.items()}},
            )
            return
        if isinstance(update, SessionInfoUpdate):
            return
        try:
            payload = update.model_dump(
                mode="json", by_alias=True, exclude_none=True
            )
        except Exception:  # noqa: BLE001 — non-pydantic objects skip persistence
            return
        if not isinstance(payload, dict):
            return
        self._record(thread_id, {"type": "update", "update": payload})

    @staticmethod
    def _sort_sessions_by_updated(
        sessions: list[SessionInfo],
    ) -> list[SessionInfo]:
        """Sort ``sessions`` by ``updated_at`` desc, missing values last.

        Stable: a row with no ``updated_at`` lands after every row that
        has one. Tie-breaks preserve input order (the caller's natural
        merge order — threads first, runs after). ISO-8601 timestamps
        sort lexically the same as chronologically, so ``reverse=True``
        on the (group, ts) tuple gives newest-first.
        """

        def _key(s: SessionInfo) -> tuple[int, str]:
            ts = s.updated_at
            if ts:
                return (0, str(ts))
            return (1, "")

        return sorted(sessions, key=_key, reverse=True)

    async def _load_thread_session(
        self, cwd: str, session_id: str
    ) -> LoadSessionResponse:
        """Replay a persisted thread (``orch-…``) and make it live again.

        Restores the thread's config, resume id and costs; replays its records
        in file order; right after each run marker, re-projects that run from
        its own data (lifecycle, last live output of a finished run, terminal
        message) under the run's prefix; follows runs that are still going.
        Works for a thread this process already holds (Zed re-opens threads):
        replay state is reset first, so nothing is suppressed as already sent.
        """
        store = ThreadStore(Path(cwd) / ".mini-ork")
        if not store.exists(session_id):
            # The thread was recorded under the agent's fallback home.
            store = ThreadStore(self._resolve_home())
        if not store.exists(session_id):
            raise RequestError.invalid_params(
                {"message": f"unknown thread: {session_id!r}"}
            )
        records = store.read(session_id)
        self._sessions[session_id] = cwd
        self._thread_sessions.add(session_id)
        self._first_prompt_sent.add(session_id)  # no new title on the next prompt
        cfg = self._thread_config.setdefault(
            session_id, self._initial_thread_config(cwd)
        )
        for rec in records:
            if rec.get("type") == "config":
                for key in ("mode", "model", "recipe", "workspace"):
                    if rec.get(key):
                        cfg[key] = str(rec[key])
            elif rec.get("type") == "claude_session" and rec.get("id"):
                cfg["claude_session_id"] = str(rec["id"])
            elif rec.get("type") == "costs" and isinstance(rec.get("costs"), dict):
                # Before the replay: re-projected runs refresh their own entries.
                self._thread_costs[session_id] = {
                    str(k): float(v) for k, v in rec["costs"].items()
                }
        self._usage_sent.pop(session_id, None)
        # Zed S1: restore the thread's base title from the first user
        # prompt's text (the live ``_prompt_thread`` does the same when
        # it first emits). The ``title`` records are the source of
        # truth for ``list_threads`` but are NOT the base — they
        # already include the live state mark (``● base``, ``✓ base``)
        # and prepending another mark would double-prefix.
        first_user_text: str | None = None
        last_title_text: str | None = None
        for rec in records:
            rtype = rec.get("type")
            if rtype == "user" and isinstance(rec.get("text"), str) and first_user_text is None:
                first_user_text = rec["text"]
            elif rtype == "title" and isinstance(rec.get("title"), str):
                last_title_text = rec["title"]
        if first_user_text:
            self._thread_titles[session_id] = title_from_text(first_user_text)
        # Seed the title-state dedup with the last persisted title so
        # a re-projection that lands the same state is a no-op emit.
        if last_title_text is not None:
            self._last_title_sent[session_id] = last_title_text
        # Reset the needs-you dedup so a fresh state transition fires
        # its message (the persisted title record is the title, not
        # the agent-message detail).
        self._last_needs_you_sent.pop(session_id, None)
        running: list[str] = []
        self._replaying.add(session_id)
        try:
            for rec in records:
                rtype = rec.get("type")
                if rtype == "user" and isinstance(rec.get("text"), str):
                    await self._emit(
                        session_id,
                        UserMessageChunk(
                            session_update="user_message_chunk",
                            content=TextContentBlock(type="text", text=rec["text"]),
                        ),
                    )
                    continue
                if rtype != "update" or not isinstance(rec.get("update"), dict):
                    continue
                try:
                    update = SessionNotification.model_validate(
                        {"sessionId": session_id, "update": rec["update"]}
                    ).update
                except Exception:  # noqa: BLE001 — skip an unreadable record
                    continue
                await self._emit(session_id, update)
                tool_call_id = str(getattr(update, "tool_call_id", "") or "")
                if isinstance(update, ToolCallStart) and tool_call_id.endswith(":parent"):
                    run_id = tool_call_id[: -len(":parent")]
                    # Z5: rebuild ``_thread_runs`` from the replayed markers
                    # in order so a re-loaded thread sees the same
                    # most-recent run as the live session that wrote them.
                    # Dedup: append only the first time we see a run id —
                    # the load's marker replay and any subsequent replay
                    # of the same marker would otherwise duplicate.
                    if _is_safe_token(run_id):
                        runs = self._thread_runs.setdefault(session_id, [])
                        if run_id not in runs:
                            runs.append(run_id)
                    if _is_safe_token(run_id) and not await self._replay_run_in_thread(
                        session_id, run_id, cwd
                    ):
                        running.append(run_id)
            await self._emit(session_id, self._thread_usage_update(session_id))
        finally:
            self._replaying.discard(session_id)
        for run_id in running:
            follower = self._followers.get(run_id)
            if follower is None or follower.done():
                self._followers[run_id] = asyncio.create_task(
                    self._follow_child(session_id, run_id)
                )
        await self._emit_available_commands(session_id)
        return LoadSessionResponse(
            config_options=cast(Any, self._current_config_options(session_id)),
        )

    async def _replay_run_in_thread(self, thread_id: str, run_id: str, cwd: str) -> bool:
        """Re-project one of a thread's runs into it; True when the run has ended."""
        self._routes[run_id] = (thread_id, f"{run_id}:")
        self._sessions.setdefault(run_id, cwd)
        # A clean replay: forget what this process already sent for the run.
        self._emitted.pop(run_id, None)
        self._tails.pop(run_id, None)
        # Z4: mirror the ``_emitted.pop`` reset for the diff dedup set so a
        # 2nd thread load fires the diff update again.
        self._diff_emitted = {key for key in self._diff_emitted if key[0] != run_id}
        self._text_seen = {key for key in self._text_seen if key[0] != run_id}
        # Z6: the plan dedup lives on the destination (the thread) so reset
        # it here too — without this the thread would see no plan after a
        # replay because the canonical tuple still matches what an earlier
        # projection pass already emitted.
        self._plan_emitted.pop(thread_id, None)
        # Zed S1: the title-state dedup lives on the destination (the
        # thread) so a 2nd thread load re-emits the title.
        self._last_title_sent.pop(thread_id, None)
        self._last_needs_you_sent.pop(thread_id, None)
        snapshot = (self._reader or self._read_snapshot)(run_id) or {
            "status": None,
            "events": [],
            "llm_calls": [],
        }
        await self._project_snapshot(run_id, snapshot, live=False)
        if snapshot.get("status") not in TERMINAL_STATUSES:
            # The follower drains the live output from the start.
            return False
        started = self._started_node_ids(snapshot)
        if started:
            await self._replay_live_for(run_id, sorted(started))
        # Z4: surface the run's file edits under the run's implementer node id
        # (resolved from lifecycle). Routes through ``_emit`` so the thread
        # prefix lands automatically. The status check inside
        # ``_emit_diff_update_for_loaded_run`` skips the diffs for rolled-back
        # runs and emits the dedicated message instead.
        await self._emit_diff_update_for_loaded_run(run_id, snapshot)
        return True

    def _home_for(self, session_id: str | None) -> Path:
        """Resolve the ``.mini-ork`` home for a session id.

        Step 1 — the session's bound cwd wins when ``<cwd>/.mini-ork`` is a
        directory (a CLI-started run in a different project resolves to the
        right home). Step 2 — when ``<cwd>/.mini-ork`` is missing and the
        cwd is a git linked worktree, the **main** checkout's ``.mini-ork``
        is used (a Zed linked worktree is gitignored and has no
        ``.mini-ork`` of its own). Steps 3–4 are the no-session fallback:
        constructor home → ``MINI_ORK_HOME`` → ``<process cwd>/.mini-ork``
        (``_resolve_home``).
        """
        cwd = self._sessions.get(session_id) if session_id is not None else None
        if cwd:
            candidate = Path(cwd) / ".mini-ork"
            if candidate.is_dir():
                return candidate
            linked_home = self._home_in_linked_worktree(Path(cwd))
            if linked_home is not None:
                return linked_home
        return self._resolve_home()

    def _launch(self, run_id: str, kickoff_text: str) -> dict[str, Any]:
        """Detached launch via the existing seam; the run id is the session id.

        Zed S7a: ``self._run_env[run_id]`` is merged into ``extra_env`` so
        per-run env (e.g. ``MO_ROUTING_POLICY=workflow_default`` on a race
        run) reaches the child process via ``launch_run``'s subprocess env.
        Anything stashed in ``_run_env`` survives for the run's whole life;
        callers are responsible for clearing it when the run ends.
        """
        from mini_ork.web.control import launch_run

        cwd = self._sessions.get(run_id)
        recipe = self._recipes.get(run_id) or self._recipe
        extra_env: dict[str, str] = {}
        if cwd:
            extra_env["MO_TARGET_CWD"] = cwd
        overlay = self._run_env.get(run_id) or {}
        if overlay:
            extra_env.update(overlay)
        return launch_run(
            self._home_for(run_id),
            recipe,
            kickoff_text,
            run_id=run_id,
            extra_env=extra_env or None,
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
