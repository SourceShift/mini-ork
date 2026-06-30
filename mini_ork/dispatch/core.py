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
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Sequence

from .models import DispatchRequest, DispatchResult, TokenUsage

# Parser callables turn a provider's stdout into structured telemetry. They are
# injected (not hard-wired) so the core stays provider-agnostic and unit-testable
# with a stub provider.
UsageParser = Callable[[str], TokenUsage]
CostParser = Callable[[str, TokenUsage], float]


def dispatch(
    request: DispatchRequest,
    command: Sequence[str],
    *,
    parse_usage: UsageParser | None = None,
    parse_cost: CostParser | None = None,
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
        proc = subprocess.run(
            list(command),
            input=request.prompt,  # ← prompt over stdin: structurally E2BIG-proof
            capture_output=True,
            text=True,
            env=proc_env,
            timeout=request.timeout_s,
        )
    except subprocess.TimeoutExpired:
        return DispatchResult(
            ok=False,
            rc=124,
            error=f"timeout after {request.timeout_s}s",
            model=request.model,
            duration_ms=int((time.monotonic() - start) * 1000),
        )
    except OSError as exc:
        return DispatchResult(
            ok=False,
            rc=127,
            error=f"spawn failed: {exc}",
            model=request.model,
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    duration_ms = int((time.monotonic() - start) * 1000)
    rc = proc.returncode
    stdout = proc.stdout or ""
    if rc != 0:
        return DispatchResult(
            ok=False,
            rc=rc,
            text=stdout,
            error=(proc.stderr or "")[-2000:],
            model=request.model,
            duration_ms=duration_ms,
        )

    usage = parse_usage(stdout) if parse_usage else TokenUsage()
    cost = parse_cost(stdout, usage) if parse_cost else 0.0
    return DispatchResult(
        ok=True,
        rc=0,
        text=stdout,
        model=request.model,
        usage=usage,
        cost_usd=cost,
        duration_ms=duration_ms,
    )
