"""ACP slash-command table + handlers (Zed Z5).

A command is a markdown-producing callable that never launches a run. The
agent announces the table via ``available_commands_update`` after
``session/new`` and after ``session/load``; the dispatcher (``MiniOrkAcpAgent``
methods) looks the typed ``/word`` up against ``COMMANDS`` and emits the
handler's markdown as one ``AgentMessageChunk`` then returns ``end_turn``.

External side effects — subprocess spawns, network probes — go through
module-level seams (``_spawn``, ``_run``, ``_probe``) so tests stay hermetic.
Handler failures are reported as a one-line markdown string; they never raise.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from acp.schema import (
    AvailableCommand,
    AvailableCommandInput,
    UnstructuredCommandInput,
)

from mini_ork.web.control import _is_safe_token


# ── command reply envelope (Zed S3a) ─────────────────────────────────────
# ``Handler`` returns ``str`` for text-only replies and ``CommandReply``
# when the reply carries file links. The dispatcher in
# ``MiniOrkAcpAgent._dispatch_slash`` walks the envelope and emits one
# ``AgentMessageChunk`` (text) plus one ``ResourceContentBlock`` per link.
@dataclass(frozen=True)
class CommandReply:
    """A handler's reply: markdown text plus file paths to surface as links.

    The dispatcher emits the text first, then one
    ``ResourceContentBlock(type="resource_link", uri=..., name=...)`` chunk
    per ``Path`` — Zed renders these as clickable file mentions. Existing
    handlers keep returning plain strings; the dataclass only kicks in for
    ``/recipe`` (and any future surface that wants to attach files).
    """

    text: str
    links: list[Path]


@dataclass(frozen=True)
class _OfferCopy:
    """Sentinel: ask the user to copy an engine recipe into the project (the
    agent shows Copy / Cancel buttons and does the copy)."""

    recipe_id: str


@dataclass(frozen=True)
class _RewriteToOrchestrate:
    """Sentinel: a handler wants the dispatcher to skip emitting text.

    ``_dispatch_slash`` detects this type, stores the rewritten prompt on
    the thread session, emits a one-line bridge message, and returns
    ``None`` so the caller falls through to ``_prompt_thread`` (where the
    rewritten text replaces the original user text for the orchestrator
    turn). This is how ``/recipe new`` and ``/recipe edit`` steer a
    thread-session turn toward the recipe-authoring prompt section without
    launching a run.

    ``bridge`` is the one-line message the dispatcher renders in the
    thread before the orchestrator turn runs. Recipe flows leave it
    ``None`` and fall back to the canonical "draft you'll see as diffs"
    text; ``/automation new`` sets it to a separate bridge so the user
    knows the buttons under a proposal will be Create automation rather
    than Create recipe.
    """

    intent_text: str
    recipe_id: str | None = None
    bridge: str | None = None


# ── announcement table ─────────────────────────────────────────────────────────
# ``COMMANDS`` is the same list the agent pushes to clients via
# ``available_commands_update``. ``input=`` is the SDK's wrapper for an
# unstructured hint (the text the user types after the command). Commands
# that take an argument carry the hint; commands that don't pass ``input=None``.
#
# Introspected ``AvailableCommandInput`` is a ``RootModel``; the only correct
# constructor is ``AvailableCommandInput(root=UnstructuredCommandInput(hint=...))``.


def _cmd(name: str, description: str, hint: str | None = None) -> AvailableCommand:
    """Build one ``AvailableCommand`` with the right ``input`` shape."""
    return AvailableCommand(
        name=name,
        description=description,
        input=(
            AvailableCommandInput(root=UnstructuredCommandInput(hint=hint))
            if hint
            else None
        ),
    )


COMMANDS: list[AvailableCommand] = [
    _cmd("help", "List every slash command.", None),
    _cmd(
        "run",
        "Start the selected recipe directly (threads).",
        "task description (the recipe comes from the thread's recipe picker)",
    ),
    _cmd(
        "runs",
        "List every run with state, recipe, step, time, cost, change; filters replace tabs.",
        "[state] [recipe:<id>] [n] — default 20, max 50",
    ),
    _cmd(
        "status",
        "Run card: state, steps, cost by stage, files changed, verdict, learnings.",
        "optional run id",
    ),
    _cmd(
        "learnings",
        "Show failure-mode gradients, learning records, and emergent patterns.",
        "optional text filter",
    ),
    _cmd(
        "cost",
        "Spend per recipe per day + this run + the rolling-24h budget.",
        "days (default 1)",
    ),
    _cmd("lanes", "Show the role → lane (recipe) map.", None),
    _cmd("stop", "Soft-stop the current run.", "optional run id"),
    _cmd("kill", "Hard-kill the current run.", "optional run id"),
    _cmd(
        "resume",
        "Clear a cost pause on the current run.",
        "optional run id",
    ),
    _cmd(
        "recover",
        "Spawn a detached `mini-ork recover` for the current run.",
        "optional `--from-node <id>`",
    ),
    _cmd(
        "certify",
        "Run certify against HEAD~1..HEAD in the session's repo.",
        "bug-report text (required)",
    ),
    _cmd("serve", "Probe the local mini-ork serve health endpoint.", None),
    _cmd(
        "recipes",
        "Recipe table: source, steps, grade, runs, success, avg cost.",
        "[project|engine] [text]",
    ),
    _cmd(
        "recipe",
        "Recipe card: description, steps, flow, contract, grade, track record.",
        "recipe id",
    ),
    _cmd(
        "recipe new",
        "Start a recipe authoring flow in this thread.",
        "describe what the recipe should do",
    ),
    _cmd(
        "recipe edit",
        "Edit an existing engine recipe (copies to project, then drafts).",
        "recipe id",
    ),
    _cmd(
        "workspaces",
        "List every open task workspace (run, branch, base, change, commits, age).",
        None,
    ),
    _cmd(
        "merge",
        "Merge a task workspace into its base branch.",
        "optional run id",
    ),
    _cmd(
        "discard",
        "Discard a task workspace (worktree + branch + record).",
        "optional run id",
    ),
    _cmd(
        "automations",
        "Scheduled recipe runs: when, next, last run, scheduler.",
        None,
    ),
    _cmd(
        "race",
        "Race the selected recipe on 2-3 models, each in its own worktree; keep the best verified change.",
        "[lane,lane] task — default sonnet,glm,minimax",
    ),
    _cmd(
        "automation",
        "Automation card, or run|pause|resume|delete <id>, scheduler on|off.",
        "<id> | run <id> | resume <id> | scheduler on|off",
    ),
    _cmd(
        "automation new",
        "Schedule a recipe: describe what should run and when.",
        "describe what should run and when",
    ),
    _cmd(
        "kickoff",
        "Draft a kickoff with the orchestrator, check it, then start the run.",
        "what the run should do",
    ),
]


# ── module-level seams (tests monkeypatch these) ──────────────────────────────
# Keeping subprocess / network access at module scope — not as class methods —
# matches the deferred-import pattern in ``MiniOrkAcpAgent._launch / _stop /
# _kill`` (the kickoff is explicit about it).


def _spawn(argv: list[str], *, cwd: str, env: dict[str, str], stdout_path: Path) -> subprocess.Popen:
    """Spawn ``argv`` detached under ``cwd`` with ``env``, log to ``stdout_path``.

    Mirrors ``control.launch_run``'s spawn shape (detached via
    ``start_new_session=True``) so a recover subprocess behaves the same as a
    brand-new run. The parent holds no pipe to the child.
    """
    log_fh = open(stdout_path, "ab")
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        log_fh.close()
    return proc


def _run(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    """Run ``argv`` synchronously with a ``timeout``; never raises on timeout."""
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            argv, 124, stdout="", stderr=f"timeout after {timeout}s: {exc}"
        )


def _probe(url: str, *, timeout: float = 1.0) -> bool:
    """True when ``url`` answers within ``timeout`` seconds; never raises."""
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


# ── handler signature ────────────────────────────────────────────────────────
# Each handler returns ``str`` (text-only reply) or ``CommandReply`` (text +
# file paths the dispatcher emits as ``ResourceContentBlock`` chunks). A
# handler that raises is reported as a one-line markdown string and never
# propagates. ``agent`` is the ``MiniOrkAcpAgent`` instance; ``session_id``
# is the ACP session id (a run id for run sessions, a thread id for thread
# sessions); ``arg`` is the text the user typed after the command (may be
# empty).
Handler = Callable[[Any, str, str], Awaitable[str | CommandReply]]


# ── helpers shared by handlers ───────────────────────────────────────────────


def _current_run_id(agent: Any, session_id: str, arg: str) -> str | None:
    """Resolve "the run this command acts on".

    A non-empty ``arg`` wins (caller passed a run id explicitly). In a thread
    session the most-recent run in ``agent._thread_runs`` is used; in a run
    session the session id IS the run id. Returns ``None`` when nothing
    applies (caller decides the user-facing message).
    """
    arg_token = arg.strip().split()
    if arg_token and not arg_token[0].startswith("-"):
        # A leading token that is not a flag wins as the run id; commands like
        # /recover carry flags (`--from-node`) so the flag is checked first.
        candidate = arg_token[0]
        if candidate and _is_safe_token(candidate):
            return candidate
    if session_id in getattr(agent, "_thread_sessions", set()):
        runs = agent._thread_runs.get(session_id) or []
        if runs:
            return runs[-1]
        return None
    return session_id


def _resolve_run_row(agent: Any, run_id: str) -> dict[str, Any] | None:
    """The ``task_runs`` row for ``run_id`` (or ``None`` when absent)."""
    try:
        from mini_ork.web.deps import db_for
        from mini_ork.web.repositories import RunDetailRepository

        home = agent._home_for(run_id)
        return RunDetailRepository(db_for(home)).fetch_task_run_row(run_id)
    except Exception:  # noqa: BLE001 — handlers never raise
        return None


def _parse_runs_arg(arg: str) -> tuple[str, str | None, int]:
    """Parse ``/runs [state] [recipe:<id>] [n]`` — order-independent.

    Each token is consumed exactly once: a ``recipe:<id>`` prefix wins,
    a parseable int wins as ``n``, the remaining state token must match
    the filter set (with ``needs-you`` accepted alongside ``needs_you``).
    Unknown tokens fall through; the caller treats the resulting state as
    ``"all"`` rather than erroring. ``limit`` is clamped to ``[1, 50]``;
    the default matches the kickoff's ``n=20``.
    """
    state = "all"
    recipe: str | None = None
    limit = 20
    for tok in arg.strip().split():
        if tok.startswith("recipe:"):
            rest = tok[len("recipe:"):].strip()
            if rest:
                recipe = rest
            continue
        if tok in ("all", "working", "needs-you", "needs_you", "done", "failed"):
            state = tok.replace("-", "_")
            continue
        try:
            limit = max(1, min(50, int(tok)))
        except ValueError:
            continue
    return state, recipe, limit


def _parse_recipes_arg(arg: str) -> tuple[str, str]:
    """Parse ``/recipes [project|engine] [text]`` — order-independent.

    The first token wins as the source filter when it matches
    ``project|engine|all``; the remaining tokens are joined as the
    case-insensitive free-text needle. An empty arg → ``("all", "")``.
    """
    source = "all"
    text_tokens: list[str] = []
    state_seen = False
    for tok in arg.strip().split():
        if not state_seen and tok in ("project", "engine", "all"):
            source = tok
            state_seen = True
            continue
        text_tokens.append(tok)
    return source, " ".join(text_tokens)


# ── handlers ─────────────────────────────────────────────────────────────────


async def handle_help(agent: Any, session_id: str, arg: str) -> str:
    """List every announced command with its description."""
    del agent, session_id, arg
    lines = ["Available slash commands:", ""]
    for cmd in COMMANDS:
        hint = ""
        if cmd.input is not None and getattr(cmd.input, "root", None) is not None:
            hint = f" — {cmd.input.root.hint}"
        lines.append(f"- `/{cmd.name}`{hint}: {cmd.description}")
    return "\n".join(lines)


async def handle_runs(agent: Any, session_id: str, arg: str) -> str:
    """Fleet view: tab counts + filtered table + filter-hint footer."""
    from mini_ork.acp import fleet

    state_filter, recipe_filter, limit_n = _parse_runs_arg(arg)
    home = agent._home_for(session_id)
    now = int(time.time())
    try:
        rows, counts = fleet.fleet_rows(
            home, state=state_filter, recipe=recipe_filter, limit=limit_n
        )
    except Exception as exc:  # noqa: BLE001 — handler must never raise
        return f"`/runs` failed: {exc}"
    return fleet.render_fleet(rows, counts, state=state_filter, now=now)


async def handle_status(agent: Any, session_id: str, arg: str) -> str:
    """Run card: state, steps, cost by stage, files changed, verdict, learnings."""
    from mini_ork.acp import fleet

    run_id = _current_run_id(agent, session_id, arg)
    if not run_id:
        return "No run in this thread yet — `/runs` lists the project's runs."
    home = agent._home_for(run_id)
    try:
        card = fleet.run_card(home, run_id)
    except Exception as exc:  # noqa: BLE001
        return f"`/status` failed: {exc}"
    if card is None:
        return f"No run {run_id} in this project."
    serve_url: str | None = None
    port = int(os.environ.get("MO_SERVE_PORT", "7090") or 7090)
    if _probe(f"http://127.0.0.1:{port}/health", timeout=1.0):
        serve_url = f"http://127.0.0.1:{port}"
    now = int(time.time())
    return fleet.render_card(card, now=now, serve_url=serve_url)


async def handle_learnings(agent: Any, session_id: str, arg: str) -> str:
    """Failure-mode gradients + learning records + emergent patterns."""
    from mini_ork.web.deps import db_for
    from mini_ork.web.repositories import LearningRepository, RunDetailRepository

    filter_text = arg.strip().lower()
    run_id = _current_run_id(agent, session_id, "")
    if not run_id:
        return "No run in this thread yet — `/runs` lists the project's runs."
    home = agent._home_for(run_id)
    try:
        learn_repo = LearningRepository(db_for(home))
        run_repo = RunDetailRepository(db_for(home))
    except Exception as exc:  # noqa: BLE001
        return f"`/learnings` failed: {exc}"

    def _matches(text: Any) -> bool:
        if not filter_text:
            return True
        return filter_text in str(text or "").lower()

    sections: list[str] = []
    try:
        row = run_repo.fetch_task_run_row(run_id)
        task_class = (row or {}).get("task_class") or ""
        if task_class:
            gradients = learn_repo.fetch_failure_mode_gradients(task_class)
            gradients = [g for g in gradients if _matches(g.get("signal") or g.get("suggested_change") or g.get("target"))][:10]
            sections.append(
                "## Failure-mode gradients\n"
                + ("\n".join(f"- `{g.get('target')}` — {g.get('signal')}" for g in gradients) or "none recorded yet")
            )
        else:
            sections.append("## Failure-mode gradients\n_(no task_class on this run yet)_")
        records = learn_repo.fetch_learning_records(run_id)
        records = [r for r in records if _matches(r.get("title") or r.get("category") or r.get("patch_summary"))][:10]
        sections.append(
            "## Learning records\n"
            + ("\n".join(f"- `{r.get('category')}` — {r.get('title')}" for r in records) or "none recorded yet")
        )
    except Exception as exc:  # noqa: BLE001
        sections.append(f"_(read-model query failed: {exc})_")

    # Emergent patterns come from a separate route handler that takes the
    # StateDB directly. Live errors are downgraded to "none recorded yet"
    # rather than blowing the whole reply.
    try:
        from mini_ork.web.routes.learning import emergent_patterns

        patterns = emergent_patterns(db_for(home), 10)
        patterns = [p for p in patterns if _matches(p.get("cluster_label") or p.get("suggested_meta_adr"))][:10]
        sections.append(
            "## Emergent patterns\n"
            + ("\n".join(f"- `{p.get('cluster_label')}` (strength {float(p.get('strength_score') or 0.0):.2f})" for p in patterns) or "none recorded yet")
        )
    except Exception as exc:  # noqa: BLE001
        sections.append(f"## Emergent patterns\n_(read failed: {exc})_")

    return "\n\n".join(sections)


async def handle_cost(agent: Any, session_id: str, arg: str) -> str:
    """Spend per recipe per day + this run + rolling-24h budget gauge."""
    from mini_ork.cost_ledger import spent_last_24h
    from mini_ork.web.deps import db_for
    from mini_ork.web.routes.trajectory import cost_by_day

    tokens = arg.strip().split()
    try:
        days = max(1, int(tokens[0])) if tokens else 1
    except (TypeError, ValueError):
        days = 1
    home = agent._home_for(session_id)
    try:
        db = db_for(home)
        rows = cost_by_day(db)
    except Exception as exc:  # noqa: BLE001
        return f"`/cost` failed: {exc}"

    # ``cost_by_day`` has one row per (day, recipe) over the whole history;
    # keep the last ``days`` calendar days (UTC).
    first_day = (_dt.datetime.now(_dt.timezone.utc).date() - _dt.timedelta(days=days - 1)).isoformat()
    recent = [r for r in rows if str(r.get("day") or "") >= first_day]
    total = sum(float(r.get("cost") or 0.0) for r in recent)
    budget = float(os.environ.get("MO_DAILY_BUDGET_USD", "50") or 50.0)
    spent_24h = spent_last_24h(db.db_path if hasattr(db, "db_path") else None)
    remaining = max(0.0, budget - spent_24h)
    run_id = _current_run_id(agent, session_id, "")
    this_run_cost = 0.0
    if run_id:
        row = _resolve_run_row(agent, run_id)
        this_run_cost = float((row or {}).get("cost_usd") or 0.0)
    lines = [
        f"## Cost (last {days} day{'s' if days != 1 else ''})",
        "",
        "| day | recipe | cost | runs |",
        "| --- | --- | --- | --- |",
    ]
    for r in recent or [{"day": "(none)", "recipe": "—", "cost": 0.0, "run_count": 0}]:
        lines.append(
            f"| {r.get('day') or '-'} | {r.get('recipe') or '-'} | "
            f"${float(r.get('cost') or 0.0):.2f} | {r.get('run_count') or 0} |"
        )
    lines.append("")
    lines.append(f"- **total**: ${total:.2f}")
    if run_id:
        lines.append(f"- **this run** (`{run_id}`): ${this_run_cost:.2f}")
    lines.append(f"- **rolling 24h**: ${spent_24h:.2f} / ${budget:.2f}  (${remaining:.2f} left)")
    return "\n".join(lines)


async def handle_lanes(agent: Any, session_id: str, arg: str) -> str:
    """Role → lane map (the routes the orchestrator and recipes use)."""
    from mini_ork.web.recipes import load_lanes

    del arg
    home = agent._home_for(session_id)
    try:
        lanes = load_lanes(home)
    except Exception as exc:  # noqa: BLE001
        return f"`/lanes` failed: {exc}"
    if not lanes:
        return "No lanes configured."
    lines = ["## Lanes", "", "| role | lane |", "| --- | --- |"]
    for role, family in sorted(lanes.items()):
        lines.append(f"| `{role}` | `{family}` |")
    return "\n".join(lines)


async def handle_recipes(agent: Any, session_id: str, arg: str) -> str:
    """Recipe table: source, steps, grade, runs, success %, avg cost."""
    from mini_ork.acp import recipe_view

    source, text = _parse_recipes_arg(arg)
    home = agent._home_for(session_id)
    try:
        rows = recipe_view.recipe_rows(home, source=source, text=text)
        return recipe_view.render_recipes(rows, source=source)
    except Exception as exc:  # noqa: BLE001 — handler must never raise
        return f"`/recipes` failed: {exc}"


async def handle_recipe(agent: Any, session_id: str, arg: str) -> CommandReply:
    """Recipe card + file links. Unknown id returns a one-line ``str``."""
    from mini_ork.acp import recipe_view

    recipe_id = arg.strip()
    if not recipe_id:
        return CommandReply(
            text="Usage: `/recipe <id>` (try `/recipes` to list them).",
            links=[],
        )
    home = agent._home_for(session_id)
    try:
        card = recipe_view.recipe_card(home, recipe_id)
    except Exception as exc:  # noqa: BLE001
        return CommandReply(text=f"`/recipe` failed: {exc}", links=[])
    if card is None:
        return CommandReply(
            text=f"No recipe {recipe_id}. `/recipes` lists them.",
            links=[],
        )
    try:
        text, files = recipe_view.render_recipe_card(card)
    except Exception as exc:  # noqa: BLE001
        return CommandReply(text=f"`/recipe` failed: {exc}", links=[])
    return CommandReply(text=text, links=files)


async def _act_on_run(
    agent: Any,
    session_id: str,
    arg: str,
    *,
    action: Callable[..., dict[str, Any]],
    action_label: str,
) -> str:
    """Shared body for ``/stop``, ``/kill``, ``/resume`` — soft/hard/clear."""
    run_id = _current_run_id(agent, session_id, arg)
    if not run_id:
        return "No run in this thread yet — `/runs` lists the project's runs."
    try:
        from mini_ork.web.deps import db_for

        home = agent._home_for(run_id)
        db = db_for(home)
        if action_label == "resume":
            result = action(home, run_id, "zed")
        else:
            result = action(home, db, run_id)
    except Exception as exc:  # noqa: BLE001
        return f"`/{action_label}` failed: {exc}"
    ok = bool(result.get("ok")) if isinstance(result, dict) else False
    if action_label == "resume" and ok is False:
        return "no cost pause on this run."
    if not ok:
        err = (result.get("error") if isinstance(result, dict) else None) or "unknown error"
        return f"`/{action_label}` failed: {err}"
    note = (result.get("note") if isinstance(result, dict) else "") or ""
    extra = f" — {note}" if note else ""
    return f"`/{action_label}` `{run_id}` ok{extra}"


async def handle_stop(agent: Any, session_id: str, arg: str) -> str:
    """Soft-stop the current run."""
    from mini_ork.web.control import stop_run

    return await _act_on_run(
        agent, session_id, arg, action=stop_run, action_label="stop"
    )


async def handle_kill(agent: Any, session_id: str, arg: str) -> str:
    """Hard-kill the current run."""
    from mini_ork.web.control import kill_run

    return await _act_on_run(
        agent, session_id, arg, action=kill_run, action_label="kill"
    )


async def handle_resume(agent: Any, session_id: str, arg: str) -> str:
    """Clear a cost pause on the current run."""
    from mini_ork.web.control import resume_cost_run

    return await _act_on_run(
        agent, session_id, arg, action=resume_cost_run, action_label="resume"
    )


async def handle_recover(agent: Any, session_id: str, arg: str) -> str:
    """Spawn a detached ``mini-ork recover`` for the current run."""
    run_id = _current_run_id(agent, session_id, arg)
    if not run_id:
        return "No run in this thread yet — `/runs` lists the project's runs."
    # Parse ``--from-node <id>`` out of the arg; the run id (if any) was
    # already consumed by ``_current_run_id``.
    tokens = [t for t in arg.split() if t]
    from_node = ""
    cleaned: list[str] = []
    skip = False
    for tok in tokens:
        if skip:
            from_node = tok
            skip = False
            continue
        if tok == "--from-node":
            skip = True
            continue
        if tok.startswith("--from-node="):
            from_node = tok[len("--from-node=") :]
            continue
        cleaned.append(tok)
    home = agent._home_for(run_id)
    # The recipe and engine root — ``control.launch_run`` resolves them
    # identically; we mirror the shape so ``/recover`` and ``launch_run``
    # share their spawn hygiene (drop MINI_ORK_VENV_ACTIVE, start_new_session).
    from mini_ork.web.control import _mini_ork_root

    root = _mini_ork_root()
    inbox = home / "runs-inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    log_path = inbox / f"{run_id}.recover.log"
    argv: list[str] = [sys.executable, str(root / "bin" / "mini-ork"), "recover", run_id]
    if from_node:
        argv.extend(["--from-node", from_node])
    env = dict(os.environ)
    env["MINI_ORK_HOME"] = str(home)
    env["MINI_ORK_ROOT"] = str(root)
    env.pop("MINI_ORK_VENV_ACTIVE", None)
    try:
        proc = _spawn(argv, cwd=str(root), env=env, stdout_path=log_path)
    except OSError as exc:
        return f"`/recover` spawn failed: {exc}"
    return (
        f"`/recover` `{run_id}` started.\n"
        f"- pid: `{proc.pid}`\n"
        f"- log: `{log_path}`\n"
        f"- argv: `{' '.join(argv)}`"
    )


async def handle_certify(agent: Any, session_id: str, arg: str) -> str:
    """Run ``mini-ork certify`` against HEAD~1..HEAD for the session cwd."""
    text = arg.strip()
    if not text:
        return "Usage: `/certify <bug-report text>`"
    timeout = float(os.environ.get("MO_ACP_CERTIFY_TIMEOUT_S", "900") or 900)
    from mini_ork.web.control import _mini_ork_root

    root = _mini_ork_root()
    cwd = agent._sessions.get(session_id) or os.getcwd()
    argv = [
        sys.executable,
        str(root / "bin" / "mini-ork"),
        "certify",
        "--repo",
        cwd,
        "--issue",
        text,
    ]
    await agent._emit(
        session_id,
        agent._build_refusal_message("Certifying HEAD~1..HEAD — this takes 1–3 minutes."),
    )
    try:
        # A thread, not the event loop: certify takes minutes and every other
        # session and live stream of this agent must keep moving meanwhile.
        result = await asyncio.to_thread(_run, argv, timeout=timeout)
    except OSError as exc:
        return f"`/certify` spawn failed: {exc}"
    stdout = (result.stdout or "").splitlines()[:20]
    body = "\n".join(stdout).rstrip()
    if result.returncode == 0:
        verdict = "PROVEN"
    elif result.returncode == 1:
        verdict = "REFUTED"
    elif result.returncode == 2:
        verdict = "UNVERIFIED"
    else:
        stderr_tail = (result.stderr or "").splitlines()[-5:]
        return (
            f"`/certify` errored (rc={result.returncode}):\n"
            + "\n".join(stderr_tail)
            + (f"\n\n{body}" if body else "")
        )
    return f"`/certify` verdict: **{verdict}**\n\n{body}".rstrip()


async def handle_serve(agent: Any, session_id: str, arg: str) -> str:
    """Probe the local ``mini-ork serve`` health endpoint."""
    del arg
    run_id = _current_run_id(agent, session_id, "")
    port = int(os.environ.get("MO_SERVE_PORT", "7090") or 7090)
    url = f"http://127.0.0.1:{port}/health"
    if _probe(url, timeout=1.0):
        target = run_id or session_id
        return f"mini-ork serve is up: http://127.0.0.1:{port}/runs/{target}"
    return "Run `mini-ork serve` to start the web UI (not running)."


async def handle_recipe_new(agent: Any, session_id: str, arg: str) -> _RewriteToOrchestrate:
    """Thread-side: rewrite to orchestrator to draft a new recipe.

    The orchestrator reads the rewritten intent, interviews the user if
    needed, and drives ``mcp__mini-ork__draft_recipe`` end-to-end. The
    handler itself never starts a run; the sentinel tells the dispatcher
    to fall through to ``_prompt_thread`` with the rewritten text.
    """
    what = arg.strip() or "Ask what it should do."
    intent = (
        f"The user wants to create a new recipe. {what}\n"
        "Follow your recipe-creation steps. When the spec is ready, call draft_recipe; "
        "the user approves the draft with buttons under it."
    )
    return _RewriteToOrchestrate(intent_text=intent, recipe_id=None)


async def handle_automation_new(
    agent: Any, session_id: str, arg: str
) -> _RewriteToOrchestrate | str:
    """``/automation new [what]`` — schedule a recipe on a cadence.

    In a run session: return the plain string the dispatcher emits
    directly (the run-session carve-out — automations are set up in a
    mini-ork thread, not from a run).
    In a thread session: rewrite to orchestrator with the
    scheduling-intent text; the orchestrator interviews (recipe, when,
    per-run task) and calls ``propose_automation``. The agent shows the
    resulting proposal as a card with Create / Change / Discard buttons;
    ``create`` may then offer to install the OS scheduler.
    """
    if session_id not in getattr(agent, "_thread_sessions", set()):
        return (
            "Automations are set up in a mini-ork thread — start one "
            "from New Thread."
        )
    what = arg.strip() or "Ask what should run and when."
    intent = (
        f"The user wants to run a recipe on a schedule. {what} "
        "Follow your scheduling steps."
    )
    return _RewriteToOrchestrate(
        intent_text=intent,
        recipe_id=None,
        bridge=(
            "Handing this to the orchestrator. When it has a proposal "
            "you'll see it here with the buttons to create it."
        ),
    )


async def handle_kickoff(
    agent: Any, session_id: str, arg: str
) -> _RewriteToOrchestrate | str:
    """``/kickoff [task]`` — draft a kickoff the user checks before any run.

    Mirrors :func:`handle_automation_new`: thread-only carve-out, rewrite
    to orchestrator with the intent text + bridge. The orchestrator
    reads the recipe (via ``describe_recipe``), interviews the user on
    scope + success criteria + what is out of scope, then calls
    ``draft_kickoff``. The agent shows the staged draft as a new-file
    diff with the lint findings, then offers Start run / Save only /
    Change something / Discard. The user starts the run with the
    button.
    """
    if session_id not in getattr(agent, "_thread_sessions", set()):
        return (
            "Kickoffs are written in a mini-ork thread — start one "
            "from New Thread."
        )
    what = arg.strip() or "Ask what the run should do."
    # The orchestrator cannot see the thread's pickers, so name the recipe.
    cfg = (getattr(agent, "_thread_config", {}) or {}).get(session_id) or {}
    recipe = str(cfg.get("recipe") or getattr(agent, "_recipe", "") or "code-fix")
    intent = (
        f"The user wants a kickoff for the {recipe} recipe (the thread's Recipe "
        f"picker; suggest another if it fits better): {what} "
        "Follow your kickoff steps."
    )
    return _RewriteToOrchestrate(
        intent_text=intent,
        recipe_id=None,
        bridge=(
            "Handing this to the orchestrator. When the kickoff is ready "
            "you'll see it here with the buttons to start the run."
        ),
    )


async def handle_recipe_edit(agent: Any, session_id: str, arg: str) -> str | _RewriteToOrchestrate:
    """Thread-side: rewrite to orchestrator to edit an existing recipe.

    Resolves ``arg`` against the recipe catalog. Unknown ids return a
    plain string (the dispatcher emits it directly). For known engine
    recipes we ask the orchestrator to copy-to-project first, then draft.
    For project recipes we go straight to draft.
    """
    from mini_ork.recipes_catalog import find_recipe

    recipe_id = arg.strip()
    if not recipe_id:
        return "Usage: `/recipe edit <id>` — recipe id required."
    home = agent._home_for(session_id)
    try:
        entry = find_recipe(recipe_id, home)
    except Exception as exc:  # noqa: BLE001
        return f"`/recipe edit` failed: {exc}"
    if entry is None:
        return f"No recipe {recipe_id!r}. `/recipes` lists them."

    if entry.source == "engine":
        return _OfferCopy(recipe_id=recipe_id)
    if not (entry.path / "recipe.spec.json").is_file():
        # Not authored from a spec: edit its files directly.
        files = sorted(p for p in entry.path.rglob("*") if p.is_file() and p.suffix in (".yaml", ".md", ".py", ".sh"))
        return CommandReply(
            text=f"`{recipe_id}` wasn't created from a spec, so edit its files directly — "
                 "the links below open them.",
            links=files,
        )
    intent = (
        f"The user wants to change recipe `{recipe_id}`. Call get_recipe_spec, ask what to "
        f"change, then call draft_recipe with base=\"{recipe_id}\"; the user approves the "
        "draft with buttons under it."
    )
    return _RewriteToOrchestrate(intent_text=intent, recipe_id=recipe_id)


# ── /workspaces / /merge / /discard (Zed S5) ──────────────────────────────────


def _resolve_review_run_id(agent: Any, session_id: str, arg: str) -> str | None:
    """Same resolution as ``_current_run_id`` for the S5 slash commands.

    Returns ``None`` when no run applies. Thread sessions resolve to
    their latest followed run unless the user typed an explicit run id;
    run sessions resolve to the session id.
    """
    return _current_run_id(agent, session_id, arg)


def _format_merge_message(base_branch: str, result: dict[str, Any]) -> str:
    """``Merged into <branch> (<fast-forward|merge commit> <short sha>).``

    The wording picks ``fast-forward`` vs ``merge commit`` from
    ``result["mode"]`` (literal set the kickoff pins:
    ``{"fast-forward", "merge"}``, :workspaces.py:283,289). Missing sha
    collapses to ``—`` so a nothing-to-merge merge still reads cleanly.
    """
    merged = result.get("merged") or ""
    short = str(merged)[:7] if merged else ""
    mode = str(result.get("mode") or "merge")
    wording = "fast-forward" if mode == "fast-forward" else "merge commit"
    sha = short or "—"
    return f"Merged into {base_branch} ({wording} {sha})."


def _merge_message(agent: Any, session_id: str, run_id: str) -> str:
    """The commit message used by ``/merge`` (Zed S6b-1 / S5).

    Reads ``agent._thread_titles[session_id]`` (thread title) and
    ``agent._run_base_titles[run_id]`` (kickoff-derived title); falls
    back to ``""`` when both are absent. The agent's
    ``_handle_review_decision`` builds the same string inline (see
    ``agent.py:1908-1909``); this helper centralises the logic for
    :func:`handle_merge` so the agent file stays untouched.

    Both attributes are read via :func:`getattr` with a default of
    ``{}`` so a partial-mock agent in tests does not blow up.
    """
    titles_by_thread = getattr(agent, "_thread_titles", {}) or {}
    titles_by_run = getattr(agent, "_run_base_titles", {}) or {}
    base = titles_by_thread.get(session_id) or titles_by_run.get(run_id) or ""
    return f"{base} (mini-ork {run_id})" if base else f"mini-ork run {run_id}"


async def handle_workspaces(agent: Any, session_id: str, arg: str) -> str:
    """``/workspaces`` — table of every open task workspace.

    Reads ``workspaces.list_open(home)`` and surfaces each row's
    branch / base / change / commits / age. Empty → "No open task
    workspaces." Footer hints at ``/merge`` / ``/discard``.
    """
    del arg
    from mini_ork import workspaces as _workspaces

    home = agent._home_for(session_id)
    items = _workspaces.list_open(home)
    if not items:
        return "No open task workspaces."
    rows: list[str] = []
    for ws in items:
        try:
            snap = _workspaces.status(ws)
        except Exception:  # noqa: BLE001 — a per-row failure must not kill the table
            snap = {}
        commits = int(snap.get("commits_ahead") or 0)
        added = int(snap.get("added") or 0)
        removed = int(snap.get("removed") or 0)
        rows.append(
            f"| `{ws.run_id}` | `{ws.branch}` | `{ws.base_branch}` | "
            f"+{added} −{removed} | {commits} | - |"
        )
    header = "| run | branch | base | change | commits | age |"
    sep = "|---|---|---|---|---|---|"
    body = "\n".join([header, sep, *rows])
    return f"Open task workspaces:\n\n{body}\n\n`/merge <run>` or `/discard <run>` to resolve."


async def handle_merge(agent: Any, session_id: str, arg: str) -> str:
    """``/merge [run]`` — fast-forward the run's workspace branch into its base."""
    from mini_ork import workspaces as _workspaces

    run_id = _resolve_review_run_id(agent, session_id, arg)
    if not run_id:
        return "No run in this thread yet — `/runs` lists the project's runs."
    home = agent._home_for(run_id)
    ws = _workspaces.load(home, run_id)
    if ws is None:
        return f"Run {run_id} has no open workspace."
    message = _merge_message(agent, session_id, run_id)
    try:
        result = _workspaces.merge(ws, message=message)
    except Exception as exc:  # noqa: BLE001
        return f"`/merge` failed: {exc}"
    if not isinstance(result, dict) or not result.get("ok"):
        err = (result.get("error") if isinstance(result, dict) else None) or "unknown error"
        return f"`/merge` failed: {err}. The worktree is kept — fix it, then /merge {run_id}."
    return _format_merge_message(ws.base_branch, result)


async def handle_discard(agent: Any, session_id: str, arg: str) -> str:
    """``/discard [run]`` — remove the run's worktree + branch + record."""
    from mini_ork import workspaces as _workspaces

    run_id = _resolve_review_run_id(agent, session_id, arg)
    if not run_id:
        return "No run in this thread yet — `/runs` lists the project's runs."
    home = agent._home_for(run_id)
    ws = _workspaces.load(home, run_id)
    if ws is None:
        return f"Run {run_id} has no open workspace."
    try:
        result = _workspaces.discard(ws)
    except Exception as exc:  # noqa: BLE001
        return f"`/discard` failed: {exc}"
    if not isinstance(result, dict) or not result.get("ok"):
        err = (result.get("error") if isinstance(result, dict) else None) or "unknown error"
        return f"`/discard` failed: {err}"
    return f"Discarded {run_id} — its worktree and branch are gone."


# ── /automations / /automation … (Zed S6b-1) ─────────────────────────────────
#
# ``HANDLERS`` keys must be explicit so the longest-match dispatcher in
# ``MiniOrkAcpAgent._dispatch_slash`` does not let a bare key shadow a
# compound one (e.g. ``automation scheduler`` would otherwise eat
# ``/automation scheduler on`` because ``body.startswith("automation scheduler ")``
# is true).


def _automation_id_from_arg(arg: str) -> str | None:
    """Extract the first non-flag token from ``arg`` (or ``None`` when empty)."""
    for tok in arg.strip().split():
        if not tok.startswith("-"):
            return tok
    return None


async def handle_automations(agent: Any, session_id: str, arg: str) -> str:
    """``/automations`` — table of every automation + scheduler footer."""
    del arg
    from mini_ork.acp import automation_view as av

    home = agent._home_for(session_id)
    try:
        return av.render_automations(home)
    except Exception as exc:  # noqa: BLE001 — handler must never raise
        return f"`/automations` failed: {exc}"


async def handle_automation(agent: Any, session_id: str, arg: str) -> str:
    """``/automation <id>`` — card, or empty → table fallback."""
    from mini_ork.acp import automation_view as av
    from mini_ork import automations as _auto

    home = agent._home_for(session_id)
    aid = arg.strip()
    if not aid:
        try:
            return av.render_automations(home)
        except Exception as exc:  # noqa: BLE001
            return f"`/automation` failed: {exc}"
    try:
        items = _auto.load(home)
    except Exception as exc:  # noqa: BLE001
        return f"`/automation` failed: {exc}"
    if not any(a.get("id") == aid for a in items):
        return f"No automation {aid}. `/automations` lists them."
    try:
        card = av.automation_card(home, aid)
    except Exception as exc:  # noqa: BLE001
        return f"`/automation` failed: {exc}"
    if card is None:
        return f"No automation {aid}. `/automations` lists them."
    try:
        return av.render_automation_card(card)
    except Exception as exc:  # noqa: BLE001
        return f"`/automation` failed: {exc}"


async def handle_automation_run(agent: Any, session_id: str, arg: str) -> str:
    """``/automation run <id>`` — fire immediately."""
    from mini_ork import automations as _auto

    aid = _automation_id_from_arg(arg)
    if not aid:
        return "Which automation? `/automations` lists them."
    home = agent._home_for(session_id)
    try:
        # Off the event loop: creating the worktree (and its setup script) can
        # take minutes, and the agent must keep serving other threads.
        result = await asyncio.to_thread(_auto.fire, home, aid)
    except Exception as exc:  # noqa: BLE001
        return f"Could not start {aid}: {exc}"
    if not isinstance(result, dict) or not result.get("ok"):
        err = (result.get("error") if isinstance(result, dict) else None) or "unknown error"
        return f"Could not start {aid}: {err}"
    rid = result.get("run_id", "")
    workspace = result.get("workspace") or "worktree"
    where = (
        f"in worktree `mini-ork/{rid}`" if workspace == "worktree" else "in place"
    )
    items = []
    try:
        items = _auto.load(home)
    except Exception:  # noqa: BLE001
        items = []
    name = next(
        (a.get("name") for a in items if a.get("id") == aid),
        aid,
    )
    return (
        f"Started {rid} for {name} — {where}. It is in the thread list; "
        f"`/status {rid}` shows it."
    )


async def handle_automation_pause(agent: Any, session_id: str, arg: str) -> str:
    """``/automation pause <id>`` — disable; ``enabled=False`` keeps history."""
    from mini_ork import automations as _auto

    aid = _automation_id_from_arg(arg)
    if not aid:
        return "Which automation? `/automations` lists them."
    home = agent._home_for(session_id)
    try:
        result = _auto.pause(home, aid)
    except Exception as exc:  # noqa: BLE001
        return f"`/automation pause` failed: {exc}"
    if not isinstance(result, dict) or not result.get("ok"):
        err = (result.get("error") if isinstance(result, dict) else None) or "unknown error"
        return f"`/automation pause` failed: {err}"
    items = []
    try:
        items = _auto.load(home)
    except Exception:  # noqa: BLE001
        items = []
    name = next(
        (a.get("name") for a in items if a.get("id") == aid),
        aid,
    )
    return (
        f"Paused {name} — it will not fire until `/automation resume {aid}`."
    )


async def handle_automation_resume(agent: Any, session_id: str, arg: str) -> str:
    """``/automation resume <id>`` — re-enable; report the next fire."""
    from mini_ork import automations as _auto

    aid = _automation_id_from_arg(arg)
    if not aid:
        return "Which automation? `/automations` lists them."
    home = agent._home_for(session_id)
    try:
        result = _auto.resume(home, aid)
    except Exception as exc:  # noqa: BLE001
        return f"`/automation resume` failed: {exc}"
    if not isinstance(result, dict) or not result.get("ok"):
        err = (result.get("error") if isinstance(result, dict) else None) or "unknown error"
        return f"`/automation resume` failed: {err}"
    items = []
    try:
        items = _auto.load(home)
    except Exception:  # noqa: BLE001
        items = []
    record = next((a for a in items if a.get("id") == aid), None)
    name = (record or {}).get("name") or aid
    from mini_ork.acp.automation_view import _format_next_fire

    now = _dt.datetime.now()
    fires = _auto.next_fires(str((record or {}).get("schedule") or ""), n=1, after=now)
    when_text = _format_next_fire(fires[0], now) if fires else "never (the schedule has no future time)"
    return f"Resumed {name} — next run {when_text}."


async def handle_automation_delete(agent: Any, session_id: str, arg: str) -> str:
    """``/automation delete <id>`` — remove; past runs stay in ``/runs``."""
    from mini_ork import automations as _auto

    aid = _automation_id_from_arg(arg)
    if not aid:
        return "Which automation? `/automations` lists them."
    home = agent._home_for(session_id)
    try:
        items = _auto.load(home)
    except Exception:  # noqa: BLE001
        items = []
    name = next(
        (a.get("name") for a in items if a.get("id") == aid),
        aid,
    )
    try:
        result = _auto.remove(home, aid)
    except Exception as exc:  # noqa: BLE001
        return f"`/automation delete` failed: {exc}"
    if not isinstance(result, dict) or not result.get("ok"):
        err = (result.get("error") if isinstance(result, dict) else None) or "unknown error"
        return f"`/automation delete` failed: {err}"
    return f"Deleted {name}. Its past runs stay in `/runs`."


async def handle_automation_scheduler(agent: Any, session_id: str, arg: str) -> str:
    """``/automation scheduler`` (bare) — one paragraph of scheduler state."""
    from mini_ork import automations as _auto

    home = agent._home_for(session_id)
    try:
        status = _auto.scheduler_status(home)
    except Exception as exc:  # noqa: BLE001
        return f"`/automation scheduler` failed: {exc}"
    command = status.get("command") or "(unknown)"
    log_path = status.get("log_path") or "(unknown)"
    if status.get("installed"):
        tick = status.get("last_tick")
        head = f"Scheduler on — last tick {tick}." if tick else "Scheduler on — no tick yet."
        return (f"{head} Every minute it runs `{command}` (log: `{log_path}`). "
                "`/automation scheduler off` removes it.")
    return ("Scheduler off — automations in this project do not fire on their own. "
            f"`/automation scheduler on` installs a job that runs `{command}` every "
            f"minute (log: `{log_path}`).")


async def handle_automation_scheduler_status(agent: Any, session_id: str, arg: str) -> str:
    """``/automation scheduler status`` — same shape as bare ``/automation scheduler``."""
    del arg
    return await handle_automation_scheduler(agent, session_id, "")


async def handle_automation_scheduler_on(agent: Any, session_id: str, arg: str) -> str:
    """``/automation scheduler on`` — install the LaunchAgent / crontab."""
    from mini_ork import automations as _auto

    del arg
    home = agent._home_for(session_id)
    try:
        result = _auto.install_scheduler(home)
    except Exception as exc:  # noqa: BLE001
        return f"Could not turn the scheduler on: {exc}"
    if not isinstance(result, dict) or not result.get("ok"):
        err = (result.get("error") if isinstance(result, dict) else None) or "unknown error"
        return f"Could not turn the scheduler on: {err}"
    return (
        "Scheduler on — automations in this project fire even when Zed is "
        "closed (a LaunchAgent / crontab line ticks every minute). "
        "`/automation scheduler off` removes it."
    )


async def handle_automation_scheduler_off(agent: Any, session_id: str, arg: str) -> str:
    """``/automation scheduler off`` — remove the LaunchAgent / crontab."""
    from mini_ork import automations as _auto

    del arg
    home = agent._home_for(session_id)
    try:
        result = _auto.uninstall_scheduler(home)
    except Exception as exc:  # noqa: BLE001
        return f"Could not turn the scheduler off: {exc}"
    if not isinstance(result, dict) or not result.get("ok"):
        err = (result.get("error") if isinstance(result, dict) else None) or "unknown error"
        return f"Could not turn the scheduler off: {err}"
    return "Scheduler off."


# ── dispatch table ───────────────────────────────────────────────────────────
# Maps the bare command name to its handler. ``/run`` is intentionally absent:
# ``MiniOrkAcpAgent`` routes ``/run <task>`` through ``_strip_slash_run``
# before this table is consulted.

HANDLERS: dict[str, Handler] = {
    "help": handle_help,
    "runs": handle_runs,
    "status": handle_status,
    "learnings": handle_learnings,
    "cost": handle_cost,
    "lanes": handle_lanes,
    "recipes": handle_recipes,
    "recipe": handle_recipe,
    "stop": handle_stop,
    "kill": handle_kill,
    "resume": handle_resume,
    "recover": handle_recover,
    "certify": handle_certify,
    "serve": handle_serve,
    "recipe new": handle_recipe_new,
    "recipe edit": handle_recipe_edit,
    "workspaces": handle_workspaces,
    "merge": handle_merge,
    "discard": handle_discard,
    "automations": handle_automations,
    "automation": handle_automation,
    "automation new": handle_automation_new,
    "kickoff": handle_kickoff,
    "automation run": handle_automation_run,
    "automation pause": handle_automation_pause,
    "automation resume": handle_automation_resume,
    "automation delete": handle_automation_delete,
    "automation scheduler": handle_automation_scheduler,
    "automation scheduler status": handle_automation_scheduler_status,
    "automation scheduler on": handle_automation_scheduler_on,
    "automation scheduler off": handle_automation_scheduler_off,
}


async def handle(agent: Any, session_id: str, name: str, arg: str) -> str | CommandReply:
    """Look up ``name`` in ``HANDLERS`` and return the handler's reply.

    The return type widens to ``str | CommandReply`` so a handler that wants
    to attach file links (e.g. ``/recipe``) can return them via the
    envelope; existing text-only handlers continue to return ``str``. The
    dispatcher in ``MiniOrkAcpAgent._dispatch_slash`` walks the envelope.

    Unknown names are a programmer error in this module — ``MiniOrkAcpAgent``
    already short-circuits unknowns with a fixed user-facing string.
    """
    handler = HANDLERS.get(name)
    if handler is None:
        return f"Unknown command /{name} — /help lists commands."
    try:
        return await handler(agent, session_id, arg)
    except Exception as exc:  # noqa: BLE001 — handler contract: never raise
        return f"`/{name}` failed: {exc}"


__all__ = [
    "COMMANDS",
    "CommandReply",
    "_RewriteToOrchestrate",
    "HANDLERS",
    "handle",
    "handle_recipe_new",
    "handle_recipe_edit",
    "handle_automation_new",
    "_spawn",
    "_run",
    "_probe",
]