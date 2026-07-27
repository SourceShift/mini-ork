"""Native port of the ``mo_runtime_exec`` contract (bash-removal WS7).

Semantics ported verbatim from ``lib/runtime/contract.sh`` + the ``local``
backend (``lib/runtime/local.sh``), which is the default and the only
backend the minimal agent scaffold (``mini_ork.agent.minimal``) relies on:

  * run ``cmd`` via ``bash -c`` with ``cwd`` pinned inside the child
    (the parent process's cwd is never mutated); a missing/unreachable
    ``cwd`` fails with rc 126, mirroring the bash ``cd || exit 126`` prefix;
  * stderr is merged into the captured output (bash redirects ``2>&1``
    into the outfile it then echoes);
  * the child runs in its own process group, so a timeout TERMs the whole
    group, grants a 500ms grace, then KILLs it — reaping descendants
    (subshells, sleeps) — and reports rc 124;
  * ``timeout`` is in seconds (fractional allowed); 0 waits forever;
  * trailing ``KEY=VAL`` pairs are injected into the child's environment.

Decision (WS7, recorded here per the bash-removal plan's "decision
recorded" exit): PORT, not retire and not inline. The contract is small
but non-trivial (pgid-kill timeout semantics, rc-126 cwd failure, merged
streams), so it earns a tested home in ``mini_ork/runtime/`` rather than
an anonymous inline in the agent. The opt-in ``bubblewrap``/``docker``
backends stay bash-only; both already degrade to ``local`` with a WARN
when their prerequisites are missing, and this port mirrors exactly that
fallback rather than silently changing isolation semantics. An unknown
backend name fails loudly with rc 2, as the bash source-time factory does.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

__all__ = ["exec_local", "mo_runtime_exec"]

_CD_FAIL_RC = 126
_TIMEOUT_RC = 124
_UNKNOWN_BACKEND_RC = 2
_TERM_GRACE_S = 0.5
_POLL_S = 0.05

# Backends whose bash implementations degrade to local (with a WARN) when
# their prerequisites are unavailable; the native port takes that fallback
# unconditionally until they are ported.
_BASH_ONLY_BACKENDS = ("bubblewrap", "docker")


def _warn(msg: str) -> None:
    print(f"mo_runtime_exec: WARN {msg}", file=sys.stderr)


def _kill_group(proc: subprocess.Popen) -> None:
    """TERM the child's process group, grace, then KILL — mirrors local.sh."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.monotonic() + _TERM_GRACE_S
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(_POLL_S)
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def exec_local(
    cmd: str,
    cwd: str = "",
    timeout: float = 0,
    env_kv: tuple[str, ...] = (),
) -> tuple[str, int]:
    """Native ``mo_runtime_local_exec``: run ``cmd``, return (output, rc)."""
    if cwd and not os.path.isdir(cwd):
        # bash prefix: `cd <cwd> || { echo ... >&2; exit 126; }` — stderr is
        # merged into the captured output, so surface it the same way.
        return (f"mo_runtime_local_exec: cd failed: {cwd}\n", _CD_FAIL_RC)

    env = None
    if env_kv:
        env = dict(os.environ)
        for kv in env_kv:
            key, _, val = kv.partition("=")
            env[key] = val

    proc = subprocess.Popen(  # noqa: S603
        ["bash", "-c", cmd],
        cwd=cwd or None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        # own pgid (PID == PGID), so a timeout signals every descendant —
        # the setsid/setpgrp spawner in local.sh exists for exactly this.
        start_new_session=True,
    )
    timeout_s = float(timeout or 0)
    try:
        out, _ = proc.communicate(timeout=timeout_s if timeout_s > 0 else None)
        return (out or "", proc.returncode)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        out, _ = proc.communicate()
        return (out or "", _TIMEOUT_RC)


def mo_runtime_exec(
    cmd: str,
    cwd: str = "",
    timeout: float = 0,
    env_kv: tuple[str, ...] = (),
    backend: str | None = None,
) -> tuple[str, int]:
    """The contract entry point. Resolves ``MO_RUNTIME_BACKEND`` like the
    bash source-time factory; only ``local`` is native today."""
    name = backend if backend is not None else os.environ.get("MO_RUNTIME_BACKEND", "local")
    if name in ("", "local"):
        return exec_local(cmd, cwd, timeout, env_kv)
    if name in _BASH_ONLY_BACKENDS:
        _warn(
            f"backend '{name}' is not ported to the native runtime yet; "
            "degrading to local (the same fallback its bash implementation "
            "uses when its prerequisites are missing)"
        )
        return exec_local(cmd, cwd, timeout, env_kv)
    return (f"mo_runtime_load_backend: unknown backend '{name}'", _UNKNOWN_BACKEND_RC)
