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
    "HANDLERS",
    "handle",
    "_spawn",
    "_run",
    "_probe",
]