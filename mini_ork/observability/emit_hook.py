"""Event hook — push mini-ork events to a local command (the simple tier).

Mini-ork writes every lifecycle event to the ``run_events`` table. The hook
turns those writes into a push channel without a server: point
``MINI_ORK_ON_EVENT`` at a command and mini-ork fires it after every
successful event insert. The value is a command line — an executable plus
its arguments — split with :func:`shlex.split` and executed directly
(``shell=False``), so nothing is re-interpreted by a shell.

    export MINI_ORK_ON_EVENT="/usr/local/bin/notify-hook --token …"

Each invocation receives three positional arguments: ``event_type``,
``run_id`` and ``payload_json``.

Dispatch is non-blocking: the hook is started with :class:`subprocess.Popen`
and the caller returns immediately. A daemon thread waits up to
``_HOOK_TIMEOUT_S`` (5 s) for it; a hook still running is killed as a
process group and reaped.

``node_heartbeat`` rows are written constantly (``node_heartbeat`` alone has
tens of thousands of rows), so heartbeats are opt-in: set
``MINI_ORK_ON_EVENT_HEARTBEATS=1`` to receive them.

An invalid value (not a split-able command, or an executable that does not
exist / is not executable) disables the hook for the process and prints one
warning to stderr — never the value itself, which may carry a token.

``mo_emit_hook`` is best-effort observability: it never raises, and a hook
failure can never break dispatch.

Public surface:
    hook_argv(value: str) -> list[str] | None
    mo_emit_hook(event_type: str = "unknown",
                 run_id: str = "",
                 payload_json: str = "{}") -> None
"""
from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
from typing import Optional


_HOOK_TIMEOUT_S = 5
_DEFAULT_PAYLOAD = "{}"
_DEFAULT_EVENT_TYPE = "unknown"

HEARTBEAT_EVENTS = frozenset({"node_heartbeat"})

_WARNING = ("mini-ork: MINI_ORK_ON_EVENT is not an executable command; "
            "event hook disabled")

# One warning per process: the value is invalid for every event, and the
# value itself may hold a token, so it is never echoed.
_WARNED_INVALID = False


def _resolve_hook_path(hook: str) -> str:
    """Resolve a relative ``.sh`` path whose file exists to an absolute path.

    Absolute paths and non-``.sh`` paths pass through unchanged. This is a
    legacy convenience for callers that name a script relative to the
    current directory; :func:`hook_argv` applies it to the first token only.
    """
    if hook.startswith("/"):
        return hook
    if not hook.endswith(".sh"):
        return hook
    if not os.path.isfile(hook):
        return hook
    abs_dir = os.path.abspath(os.path.join(os.getcwd(), os.path.dirname(hook)))
    return os.path.join(abs_dir, os.path.basename(hook))


def _resolve_timeout_bin() -> Optional[str]:
    """Return ``gtimeout``/``timeout`` if present on PATH, else ``None``.

    Retained for compatibility with callers and tests that probe it. The
    hook no longer wraps the command in a timeout binary — it is killed by
    the dispatcher thread instead — so this is unused by :func:`mo_emit_hook`.
    """
    if shutil.which("gtimeout") is not None:
        return "gtimeout"
    if shutil.which("timeout") is not None:
        return "timeout"
    return None


def hook_argv(value: str) -> list[str] | None:
    """Split ``value`` into an executable argv, or ``None`` when invalid.

    The value is a command line: :func:`shlex.split` it (a ``ValueError``
    from an unclosed quote yields ``None``) and require the first token to
    name an existing executable file (``os.path.isfile`` and
    ``os.access(X_OK)``) or to be resolvable with :func:`shutil.which`.
    ``None`` also covers an empty token list. Otherwise the tokens are
    returned with the first token resolved.
    """
    try:
        tokens = shlex.split(value)
    except ValueError:
        return None
    if not tokens:
        return None

    head = _resolve_hook_path(tokens[0])
    if os.path.isfile(head) and os.access(head, os.X_OK):
        tokens[0] = head
        return tokens
    found = shutil.which(head)
    if found:
        tokens[0] = found
        return tokens
    return None


def _max_inflight() -> int:
    try:
        return max(1, int(os.environ.get("MINI_ORK_ON_EVENT_MAX_INFLIGHT", "8")))
    except ValueError:
        return 8


_INFLIGHT = threading.BoundedSemaphore(_max_inflight())


def _kill_after_timeout(proc: subprocess.Popen) -> None:
    """Daemon-thread body: wait, kill the group after the cap, free the slot."""
    try:
        _wait_or_kill(proc)
    finally:
        try:
            _INFLIGHT.release()
        except ValueError:
            pass


def _wait_or_kill(proc: subprocess.Popen) -> None:
    try:
        proc.wait(timeout=_HOOK_TIMEOUT_S)
        return
    except Exception:
        pass
    # ``start_new_session=True`` made the hook its own process-group leader,
    # so killing the group also reaps anything it spawned.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.wait(timeout=_HOOK_TIMEOUT_S)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def mo_emit_hook(event_type: str = _DEFAULT_EVENT_TYPE,
                 run_id: str = "",
                 payload_json: str = _DEFAULT_PAYLOAD) -> None:
    """Fire the ``MINI_ORK_ON_EVENT`` command for one event (best-effort).

    Returns immediately after spawning the hook; a daemon thread enforces
    the 5 s cap. No-ops when the variable is unset or blank, and for
    heartbeat events unless ``MINI_ORK_ON_EVENT_HEARTBEATS=1``. An invalid
    value disables the hook and warns once per process. Never raises.
    """
    global _WARNED_INVALID
    try:
        value = os.environ.get("MINI_ORK_ON_EVENT", "")
        if not value or not value.strip():
            return
        if event_type in HEARTBEAT_EVENTS and \
                os.environ.get("MINI_ORK_ON_EVENT_HEARTBEATS") != "1":
            return

        argv = hook_argv(value)
        if argv is None:
            if not _WARNED_INVALID:
                _WARNED_INVALID = True
                try:
                    print(_WARNING, file=sys.stderr)
                except Exception:
                    pass
            return

        # A bounded number of hooks may run at once: with heartbeats opted in,
        # a busy machine must not turn every event into a process.
        if not _INFLIGHT.acquire(blocking=False):
            return
        # The timeout binary (when present) enforces the 5 s cap even if this
        # process exits first; the daemon thread is the fallback and reaps.
        timeout_bin = _resolve_timeout_bin()
        prefix = [timeout_bin, "-s", "KILL", str(_HOOK_TIMEOUT_S)] if timeout_bin else []
        try:
            proc = subprocess.Popen(
                prefix + argv + [event_type, run_id, payload_json],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            _INFLIGHT.release()
            raise
        threading.Thread(target=_kill_after_timeout, args=(proc,),
                         daemon=True).start()
    except Exception:
        return
