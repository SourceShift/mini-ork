"""The dispatch primitive — run a provider command and return a typed result.

This is the Python replacement for the part of lib/llm-dispatch.sh that kept
breaking. Two structural guarantees the bash version could not make:

  1. **No E2BIG.** The prompt is delivered on STDIN (`input=`), never as an argv
     element or environment variable. `execve()` counts argv + env against one
     ARG_MAX budget (~1 MB on macOS); the bash lanes passed the prompt/stream
     through env vars and died with "Argument list too long" on long turns.
     Over stdin there is no size limit.

  2. **Faithful rc.** The provider's exit code is read directly off the process
     object and returned. There is no `if cmd; then …; fi` (which, with no
     `else`, returns 0 even when the condition failed) to mask a hard failure as
     success.

  3. **Process contract.** The harness is spawned into its own session
     (`start_new_session=True`) so neither it nor anything it spawns has a
     controlling terminal — no `/dev/tty` prompt can block a headless run. A
     timeout SIGKILLs the whole detached process *group*, so a hung harness
     can't orphan grandchildren that keep burning the lane (and can't deadlock
     the output drain by holding the inherited stdout pipe).
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable, Sequence

from .models import DispatchRequest, DispatchResult, TokenUsage

# Parser callables turn a provider's stdout into structured telemetry. They are
# injected (not hard-wired) so the core stays provider-agnostic and unit-testable
# with a stub provider.
UsageParser = Callable[[str], TokenUsage]
CostParser = Callable[[str, TokenUsage], float]
TextParser = Callable[[str], str]


def _terminate_process_group(proc: "subprocess.Popen[str]") -> None:
    """SIGKILL the whole session the child leads. The child was spawned with
    ``start_new_session=True``, so it is its own process-group leader; killing
    the *group* — not just the direct child — reaps any grandchildren the
    harness spawned (claude's helpers, codex's sidecar). That matters twice: a
    hung lane can't leave orphans that keep burning cost after we've abandoned
    it, and it frees the inherited stdout pipe those grandchildren hold, which
    is what would otherwise deadlock the drain ``communicate()``. Best-effort —
    if the group is already gone, fall back to the direct child."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def dispatch(
    request: DispatchRequest,
    command: Sequence[str],
    *,
    parse_usage: UsageParser | None = None,
    parse_cost: CostParser | None = None,
    parse_text: TextParser | None = None,
    parse_session: "Callable[[str], str] | None" = None,
) -> DispatchResult:
    """Run ``command`` (an argv list — no shell), feeding ``request.prompt`` on
    stdin, and return a :class:`DispatchResult`.

    A non-zero exit, a timeout, or a spawn failure each yield ``ok=False`` with a
    distinct ``rc`` (the provider's code, ``124`` for timeout, ``127`` for spawn
    failure) and the captured stderr in ``error`` — the caller always gets a
    structured result, never an exception to wrangle.
    """
    proc_env = dict(os.environ)
    if request.env:
        proc_env.update({str(k): str(v) for k, v in request.env.items()})

    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=proc_env,
            cwd=request.cwd,  # None = inherit; pinned by the caller's cwd guard
            # Sever the controlling terminal: the harness — and everything it
            # spawns — becomes its own session with no /dev/tty, so no code path
            # (mini-ork's own plan.py prompt or the harness CLI) can open the
            # terminal and block a headless run. It also makes the whole harness
            # a single process group we can reap on timeout (below).
            start_new_session=True,
        )
    except OSError as exc:
        return DispatchResult(
            ok=False,
            rc=127,
            error=f"spawn failed: {exc}",
            model=request.model,
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    try:
        # Prompt over stdin (input=): structurally E2BIG-proof — never on argv/env.
        stdout, stderr = proc.communicate(
            input=request.prompt, timeout=request.timeout_s
        )
    except subprocess.TimeoutExpired:
        # A hung harness is the single biggest reliability failure. Reap its
        # whole detached group, drain the pipes to reap the zombie, then return
        # a distinct rc=124 so dispatch_with_fallback abandons this lane.
        _terminate_process_group(proc)
        proc.communicate()
        return DispatchResult(
            ok=False,
            rc=124,
            error=f"timeout after {request.timeout_s}s",
            model=request.model,
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    duration_ms = int((time.monotonic() - start) * 1000)
    rc = proc.returncode
    stdout = stdout or ""
    if rc != 0:
        # A FAILED node is exactly what E4 resumes — capture its session_id
        # from whatever envelope made it to stdout so the recovery can
        # `--resume` the same conversation.
        return DispatchResult(
            ok=False,
            rc=rc,
            text=stdout,
            error=(stderr or "")[-2000:],
            model=request.model,
            duration_ms=duration_ms,
            session_id=parse_session(stdout) if parse_session else "",
        )

    usage = parse_usage(stdout) if parse_usage else TokenUsage()
    cost = parse_cost(stdout, usage) if parse_cost else 0.0
    # parse_text extracts the assistant body from a structured envelope (e.g.
    # claude --output-format json puts it in .result); default is raw stdout.
    text = parse_text(stdout) if parse_text else stdout
    # parse_session pulls the provider conversation id (claude .session_id) so
    # a failed node can later be resumed at its interrupted turn (E4). "" for
    # providers that don't surface one.
    session_id = parse_session(stdout) if parse_session else ""
    return DispatchResult(
        ok=True,
        rc=0,
        text=text,
        model=request.model,
        usage=usage,
        cost_usd=cost,
        duration_ms=duration_ms,
        session_id=session_id,
    )
