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
from collections.abc import Callable, Mapping, Sequence

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


def spawn_local(
    argv: Sequence[str],
    *,
    stdin: str,
    timeout: float,
    env: Mapping[str, str],
    cwd: str | None,
) -> tuple[int, str, str]:
    """Host spawn primitive: run ``argv`` as a subprocess of THIS process and
    return ``(rc, stdout, stderr)`` — raw, unparsed, streams **separated**.

    This is the thin ``Workspace.spawn`` transport for the ``host`` backend, and
    the single place the A.1 process contract lives (SE-3): ``start_new_session``
    severs the controlling terminal so no ``/dev/tty`` prompt can block a
    headless run and the harness becomes one reapable process group; a timeout
    SIGKILLs that whole group and yields the conventional ``rc=124``; a failed
    ``execve`` yields ``rc=127``. The prompt rides on stdin (``input=``), never
    argv/env, so it is structurally E2BIG-proof. The separated-stream, stdin-fed
    shape is exactly what ``dispatch`` needs and what ``Workspace.exec`` (merged
    output, no stdin) deliberately cannot provide — which is why isolation of the
    CLI spawn needs this verb, not ``exec``.
    """
    try:
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(env),
            cwd=cwd,  # None = inherit; pinned by the caller's cwd guard
            start_new_session=True,
        )
    except OSError as exc:
        return 127, "", f"spawn failed: {exc}"

    try:
        # Prompt over stdin (input=): structurally E2BIG-proof — never on argv/env.
        stdout, stderr = proc.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        # A hung harness is the single biggest reliability failure. Reap its
        # whole detached group, drain the pipes to reap the zombie, then surface
        # the distinct rc=124 so dispatch_with_fallback abandons this lane.
        _terminate_process_group(proc)
        proc.communicate()
        return 124, "", f"timeout after {timeout}s"
    return proc.returncode, stdout or "", stderr or ""


def _spawn_in_workspace(
    backend: str,
    argv: Sequence[str],
    *,
    stdin: str,
    timeout: float,
    env: Mapping[str, str],
    cwd: str | None,
) -> tuple[int, str, str]:
    """Delegate a scope=agent CLI spawn to an isolation backend's
    ``Workspace.spawn`` (SE-3 hybrid: this engine owns the spawn CONTRACT; the
    backend owns only container TRANSPORT).

    ``dispatch`` owns argv/stdin/stream-shape/timeout-rc/parse; this helper only
    resolves the named ``Workspace`` and drives its one-shot lifecycle
    ``up() → spawn() → down()`` (the CLI spawn is one-shot, so the container is
    cattle: provisioned, used once, torn down). Resolution reuses
    ``agent_workspace.resolve_spawn_workspace`` so container config (image, drive
    root, mount path) stays defined in one place. Imported lazily to keep the
    dispatch core free of the runtime/sandbox import at module load. A failed
    resolve / ``up`` / ``spawn`` (e.g. a backend whose spawn is not yet
    implemented) propagates loudly — an unbuilt or misconfigured isolation
    backend is a setup error, not a retryable lane failure to mask as ok=False.
    """
    from mini_ork.runtime.agent_workspace import resolve_spawn_workspace

    ws = resolve_spawn_workspace(backend, env=env, cwd=cwd)
    ws.up()
    try:
        return ws.spawn(list(argv), stdin=stdin, timeout=timeout, env=env, cwd=cwd)
    finally:
        ws.down()


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
    # Isolation selector (SE-3 SC3): "host" keeps the exact in-process Popen
    # (zero regression); any other backend routes the CLI spawn through that
    # Workspace's spawn transport. Either way the same (rc, stdout, stderr)
    # triple flows into ONE finalize below, so timeout/spawn-fail/parse semantics
    # are defined once regardless of where the harness ran.
    if request.workspace == "host":
        rc, stdout, stderr = spawn_local(
            command,
            stdin=request.prompt,
            timeout=request.timeout_s,
            env=proc_env,
            cwd=request.cwd,
        )
    else:
        rc, stdout, stderr = _spawn_in_workspace(
            request.workspace,
            command,
            stdin=request.prompt,
            timeout=request.timeout_s,
            env=proc_env,
            cwd=request.cwd,
        )

    duration_ms = int((time.monotonic() - start) * 1000)
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
