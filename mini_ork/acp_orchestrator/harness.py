"""One conversational turn against a Claude-CLI lane.

``build_command`` mutates the lane's dispatch argv (output format, permission
mode, MCP wiring, allowed/disallowed tools, system prompt, optional resume)
and re-uses ``resolve_provider`` exactly so credential handling stays in one
place. ``run_turn`` spawns that argv as an asyncio subprocess, streams the
stream-json events to a callback, parses the final result envelope, prices
the turn via the dispatch layer's cost parser, and cleans up the temp MCP
config even on timeout or cancellation.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from mini_ork.dispatch.models import TokenUsage
from mini_ork.dispatch.providers import (
    ProviderSpec,
    apply_resume,
    claude_cost,
    mini_ork_root,
    resolve_provider,
)

__all__ = ["TurnResult", "build_command", "run_turn"]


# Tools the orchestrator is allowed to invoke. The kickoff grants read-only
# repo access (the orchestrator must never edit — all edits go through a
# mini-ork run) plus the full mini-ork MCP tool namespace, prefixed
# ``mcp__mini-ork__`` (the MCP server's tools re-export under that prefix).
_ALLOWED_TOOLS = ("mcp__mini-ork__*", "Read", "Grep", "Glob", "LS")

# Tools the orchestrator must NOT invoke. ``Edit`` / ``Write`` / ``MultiEdit``
# / ``NotebookEdit`` would let it bypass the mini-ork change contract; ``Bash``
# would let it bypass the MCP sandbox and exec anything in the host shell.
_DISALLOWED_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash")

# Cancellation grace period for SIGTERM → SIGKILL escalation.
_SIGTERM_GRACE_S = 3.0

# Tail length of stderr surfaced in ``TurnResult.error`` when rc != 0.
_STDERR_TAIL_CHARS = 2000


@dataclass
class TurnResult:
    """The outcome of one orchestrator turn.

    ``session_id`` is the claude CLI's conversation id; the caller passes it
    back as ``resume=`` on the next turn to continue the same conversation.
    ``text`` is the final assistant result text (empty when the run produced
    no result envelope — timeout, spawn failure, no result event).
    ``cost_usd`` is the dispatch-layer price for the turn (list price for
    non-Anthropic compat lanes, per the dispatch contract).
    ``error`` is a short human-readable failure cause; populated when ``rc``
    is non-zero or when the turn never reached a result envelope.
    """

    session_id: str | None
    rc: int
    text: str
    cost_usd: float
    error: str = ""
    raw_stdout: str = field(default="", repr=False)


# ── argv construction ───────────────────────────────────────────────────────


# claude CLI model aliases for the subscription lanes.
_SUBSCRIPTION_MODEL_ALIASES = {"opus": "opus", "sonnet": "sonnet"}


def build_command(
    lane: str,
    *,
    resume: str | None,
    mcp_config_path: Path,
    prompt_path: Path,
) -> tuple[list[str], dict[str, str]]:
    """Assemble the argv + env for one orchestrator turn.

    Starts from ``resolve_provider(lane)`` and reapplies its credential +
    model plumbing verbatim — do not re-derive. Then mutates the argv with
    the four substitutions the kickoff spec mandates:

    - ``--output-format json`` → ``--output-format stream-json --verbose``
      (the CLI requires ``--verbose`` whenever ``--print`` is paired with
      ``stream-json`` — ``providers.py:636``).
    - ``--permission-mode bypassPermissions`` → ``--permission-mode default``
      (the orchestrator must ask before destructive tool use).
    - Adds ``--mcp-config``, ``--strict-mcp-config``, ``--allowedTools``,
      ``--disallowedTools``, ``--append-system-prompt-file``.
    - Adds ``--resume <resume>`` *only* when ``resume`` is provided (via
      ``apply_resume`` so the splice lands in the right position).

    The user prompt is fed on stdin by ``run_turn``; it never appears in argv.
    """
    spec: ProviderSpec = resolve_provider(lane)
    argv = list(spec.command)
    env = dict(spec.env)

    argv = _replace_flag_pair(
        argv,
        old=("--output-format", "json"),
        new=("--output-format", "stream-json", "--verbose"),
    )
    argv = _replace_flag_pair(
        argv,
        old=("--permission-mode", "bypassPermissions"),
        new=("--permission-mode", "default"),
    )
    argv += [
        "--mcp-config",
        str(mcp_config_path),
        "--strict-mcp-config",
        "--allowedTools",
        *list(_ALLOWED_TOOLS),
        "--disallowedTools",
        *list(_DISALLOWED_TOOLS),
        "--append-system-prompt-file",
        str(prompt_path),
    ]
    # The orchestrator maps whole messages; token-level partials are noise.
    argv = [a for a in argv if a != "--include-partial-messages"]
    # The project's Claude settings and CLAUDE.md apply; the user's personal
    # ones (~/.claude: hooks, global instructions) do not — they are written
    # for the user's own sessions and leak into the thread (e.g. a "always end
    # with a status block" rule). MO_ORCHESTRATOR_SETTING_SOURCES overrides;
    # set it empty to load everything, as plain `claude` does.
    sources = os.environ.get("MO_ORCHESTRATOR_SETTING_SOURCES", "project,local").strip()
    if sources:
        argv += ["--setting-sources", sources]
    # Subscription lanes pin no model (they run the user's Claude Code default);
    # the orchestrator lane the user picked by name must actually run that model.
    if lane in _SUBSCRIPTION_MODEL_ALIASES and "--model" not in argv and not env.get("ANTHROPIC_MODEL"):
        argv += ["--model", _SUBSCRIPTION_MODEL_ALIASES[lane]]
    if resume:
        argv = list(apply_resume(tuple(argv), resume))
    return argv, env


def _replace_flag_pair(
    argv: list[str], *, old: Sequence[str], new: Sequence[str]
) -> list[str]:
    """Replace one (flag, value) pair with a different sequence.

    Returns a new list — never mutates the caller's argv. If the old pair is
    not found, the new sequence is appended at the end (so a downstream
    dispatch that strips the old flags still gets the orchestrator's
    substitutions). Splits on exact equality, not substring, so ``--model``
    never accidentally matches ``--model-id`` and similar.
    """
    out: list[str] = []
    i = 0
    n = len(argv)
    while i < n:
        if i + len(old) <= n and argv[i : i + len(old)] == list(old):
            out.extend(new)
            i += len(old)
            continue
        out.append(argv[i])
        i += 1
    return out


# ── turn execution ──────────────────────────────────────────────────────────


def _resolve_launcher_path(launcher: str | None) -> str:
    """Find the ``mini-ork`` executable the MCP config will invoke.

    Order: explicit ``launcher=`` arg → ``shutil.which("mini-ork")`` →
    ``~/.local/bin/mini-ork`` → ``<engine root>/bin/mini-ork``. The last
    fallback matches what the engine's own launcher script resolves to at
    install time (``bin/mini-ork`` is the committed wrapper).
    """
    if launcher:
        return os.path.abspath(launcher)
    found = shutil.which("mini-ork")
    if found:
        return os.path.abspath(found)
    home = Path.home() / ".local" / "bin" / "mini-ork"
    if home.is_file():
        return str(home.resolve())
    engine_bin = mini_ork_root() / "bin" / "mini-ork"
    return str(engine_bin.resolve())


def _write_mcp_config(
    home: Path, launcher: str, extra_env: Mapping[str, str] | None = None
) -> Path:
    """Write the per-turn MCP config JSON. Caller is responsible for cleanup.

    ``extra_env`` is merged into ``mcpServers.mini-ork.env`` so a thread
    session's workspace mode (e.g. ``MO_WORKSPACE_MODE=worktree``) reaches
    the spawned ``mini-ork mcp-context --control`` subprocess. The MCP
    server's own defaults (``MINI_ORK_HOME``) win on key conflict so a
    caller cannot accidentally override the contract.
    """
    # Both: bin/mini-ork prefers MINI_ORK_PROJECT_HOME, and an inherited one
    # (from a thread opened in a linked worktree) would point at a home that
    # does not exist.
    env = {"MINI_ORK_HOME": str(home), "MINI_ORK_PROJECT_HOME": str(home)}
    if extra_env:
        for key, value in extra_env.items():
            env.setdefault(str(key), str(value))
    payload = {
        "mcpServers": {
            "mini-ork": {
                "command": launcher,
                "args": ["mcp-context", "--control"],
                "env": env,
            }
        }
    }
    fd, name = tempfile.mkstemp(prefix="mcp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except Exception:
        # On write failure, release the temp file before re-raising.
        try:
            os.unlink(name)
        except OSError:
            pass
        raise
    return Path(name)


def _spawn_env(base_env: Mapping[str, str], lane_env: Mapping[str, str]) -> dict[str, str]:
    """Merge parent env + lane env, dropping the parent's venv sentinel.

    ``MINI_ORK_VENV_ACTIVE`` is the marker ``bin/mini-ork`` sets when it has
    re-exec'd into the project venv. The orchestrator's child (the ``claude``
    CLI) does NOT inherit that marker — it must resolve its own Python so a
    hung parent venv never poisons the child's imports. Drop the marker
    verbatim, matching ``mini_ork/web/control.py:687``.
    """
    merged = dict(base_env)
    for key, value in lane_env.items():
        merged[key] = value
    merged.pop("MINI_ORK_VENV_ACTIVE", None)
    return merged


def _terminate_group(pid: int) -> None:
    """SIGKILL the whole process group, falling back to the direct child.

    The child was spawned with ``start_new_session=True`` so it is its own
    process-group leader; killing the group reaps any grandchildren the
    harness spawned (claude's helpers, sidecars). If the group is already
    gone (process exited cleanly between SIGTERM and SIGKILL), fall back to
    a direct ``os.kill`` on the pid — best-effort, never raises.
    """
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


async def _stream_subprocess(
    proc: asyncio.subprocess.Process,
    on_event: Callable[[dict], Awaitable[None]],
) -> tuple[str, int, int]:
    """Drain ``proc.stdout`` line by line and tee parsed objects to the callback.

    Returns ``(raw_stdout, rc, sigterm_count)`` so the caller can build the
    final ``TurnResult``. Lines that are not JSON are skipped silently (the
    kickoff explicitly: "skip non-JSON"). Cancellation of the awaiting task
    propagates a ``CancelledError`` after the caller has terminated the
    process.
    """
    raw_parts: list[str] = []
    sigterm_count = 0
    rc = -1
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace")
        raw_parts.append(text)
        stripped = text.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(obj, dict):
            try:
                await on_event(obj)
            except Exception as exc:  # noqa: BLE001 — callback errors must not kill the drain
                print(
                    f"mini-ork.acp_orchestrator: on_event raised {exc!r}; continuing",
                    file=sys.stderr,
                )
                sigterm_count += 1
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                break
    await proc.wait()
    rc = proc.returncode if proc.returncode is not None else -1
    return "".join(raw_parts), rc, sigterm_count


async def run_turn(
    *,
    lane: str,
    prompt: str,
    cwd: Path,
    home: Path,
    resume: str | None,
    on_event: Callable[[dict], Awaitable[None]],
    timeout_s: float = 3600,
    launcher: str | None = None,
    extra_mcp_env: Mapping[str, str] | None = None,
) -> TurnResult:
    """Run one conversational turn and return its outcome.

    Spawns the ``claude`` CLI for ``lane`` with a per-turn MCP config that
    points at ``mini-ork mcp-context --control``, streams stream-json events
    to ``on_event`` as they arrive, and returns the parsed result envelope
    (session id, final text, cost). The temp MCP config is always removed,
    even on cancellation or timeout. The prompt is fed on stdin, never argv.

    ``extra_mcp_env`` is forwarded into the temp MCP config's ``env`` block
    (e.g. ``MO_WORKSPACE_MODE=worktree`` from the thread's stored config).
    Callers cannot override ``MINI_ORK_HOME`` — the harness owns that key.

    Cancellation: if the awaiting task is cancelled, terminate the process
    group (SIGTERM, SIGKILL after ``_SIGTERM_GRACE_S``) and re-raise.
    Timeout: same escalation, ``rc=124``, ``error="timeout"``.
    Spawn failure (no such executable, OSError): ``rc=127``.
    """
    launcher_path = _resolve_launcher_path(launcher)
    mcp_config_path: Path | None = None
    proc: asyncio.subprocess.Process | None = None

    async def _cleanup() -> None:
        if mcp_config_path is not None:
            try:
                mcp_config_path.unlink(missing_ok=True)
            except OSError:
                pass

    try:
        argv, lane_env = build_command(
            lane,
            resume=resume,
            mcp_config_path=Path("/__placeholder__"),
            prompt_path=Path("/__placeholder__"),
        )
        # Build the real temp MCP config + system prompt path now (avoids
        # leaking them on the ``build_command`` error path).
        mcp_config_path = _write_mcp_config(home, launcher_path, extra_env=extra_mcp_env)
        # Patch the placeholders the builder planted.
        argv[argv.index("--mcp-config") + 1] = str(mcp_config_path)
        argv[argv.index("--append-system-prompt-file") + 1] = str(
            Path(__file__).resolve().parent / "prompt.md"
        )
        merged_env = _spawn_env(os.environ, lane_env)

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                env=merged_env,
                start_new_session=True,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            await _cleanup()
            return TurnResult(
                session_id=None,
                rc=127,
                text="",
                cost_usd=0.0,
                error=f"spawn failed: {exc}",
            )

        # Feed the prompt on stdin and close it so the child sees EOF.
        if proc.stdin is not None:
            try:
                proc.stdin.write(prompt.encode("utf-8"))
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
                try:
                    await proc.stdin.wait_closed()
                except (OSError, ValueError):
                    pass

        raw_stdout = ""
        stderr_text = ""
        rc = -1
        try:
            raw_stdout, rc, _ = await asyncio.wait_for(
                _stream_subprocess(proc, on_event),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            _terminate_group(proc.pid)
            try:
                await asyncio.wait_for(proc.wait(), timeout=_SIGTERM_GRACE_S)
            except asyncio.TimeoutError:
                _terminate_group(proc.pid)
                try:
                    await proc.wait()
                except Exception:
                    pass
            if proc.stderr is not None:
                try:
                    stderr_bytes = await proc.stderr.read()
                    stderr_text = stderr_bytes.decode("utf-8", errors="replace")
                except Exception:
                    stderr_text = ""
            await _cleanup()
            return TurnResult(
                session_id=None,
                rc=124,
                text="",
                cost_usd=0.0,
                error=f"timeout: stderr tail: {_truncate(stderr_text)}",
            )
        except asyncio.CancelledError:
            _terminate_group(proc.pid)
            try:
                await asyncio.wait_for(proc.wait(), timeout=_SIGTERM_GRACE_S)
            except asyncio.TimeoutError:
                _terminate_group(proc.pid)
                try:
                    await proc.wait()
                except Exception:
                    pass
            await _cleanup()
            raise
        finally:
            if proc is not None and proc.returncode is None and proc.returncode != rc:
                # Drain any remaining stderr if we returned without timing out.
                pass

        # Drain stderr (the stream-json harness rarely writes here, but
        # the cost-free tail surfaces real errors when ``rc != 0``).
        if proc.stderr is not None:
            try:
                stderr_bytes = await proc.stderr.read()
                stderr_text = stderr_bytes.decode("utf-8", errors="replace")
            except Exception:
                stderr_text = ""

        result = _parse_result(raw_stdout)
        cost = 0.0
        try:
            # ``claude_cost`` ignores ``_usage`` (the per-call usage envelope
            # is only meaningful mid-stream; the dispatch layer reads the
            # full stream-json envelope at the end). Pass a zero TokenUsage so
            # we satisfy the type signature without lying about cost.
            cost = claude_cost(raw_stdout, TokenUsage(0, 0, 0, 0))
        except Exception:  # noqa: BLE001 — pricing must never break a turn
            cost = 0.0

        await _cleanup()

        if rc != 0 and not result["text"]:
            error = f"rc={rc}: stderr tail: {_truncate(stderr_text)}"
            return TurnResult(
                session_id=result["session_id"],
                rc=rc,
                text="",
                cost_usd=cost,
                error=error,
                raw_stdout=raw_stdout,
            )

        return TurnResult(
            session_id=result["session_id"],
            rc=rc,
            text=result["text"],
            cost_usd=cost,
            error="" if rc == 0 else f"rc={rc}",
            raw_stdout=raw_stdout,
        )
    except asyncio.CancelledError:
        if proc is not None:
            _terminate_group(proc.pid)
            try:
                await asyncio.wait_for(proc.wait(), timeout=_SIGTERM_GRACE_S)
            except asyncio.TimeoutError:
                _terminate_group(proc.pid)
                try:
                    await proc.wait()
                except Exception:
                    pass
        await _cleanup()
        raise
    except Exception as exc:  # noqa: BLE001
        if proc is not None and proc.returncode is None:
            _terminate_group(proc.pid)
            try:
                await proc.wait()
            except Exception:
                pass
        await _cleanup()
        return TurnResult(
            session_id=None,
            rc=1,
            text="",
            cost_usd=0.0,
            error=f"run_turn failed: {exc!r}",
        )


def _parse_result(raw_stdout: str) -> dict[str, Any]:
    """Pull session_id + text out of the final ``{"type":"result"}`` envelope."""
    session_id: str | None = None
    text = ""
    for line in raw_stdout.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            obj = json.loads(stripped)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") != "result":
            continue
        sid = obj.get("session_id")
        if isinstance(sid, str) and sid:
            session_id = sid
        result_text = obj.get("result")
        if isinstance(result_text, str):
            text = result_text
    return {"session_id": session_id, "text": text}


def _truncate(text: str) -> str:
    """Tail the last ``_STDERR_TAIL_CHARS`` characters of stderr."""
    if len(text) <= _STDERR_TAIL_CHARS:
        return text
    return text[-_STDERR_TAIL_CHARS:]