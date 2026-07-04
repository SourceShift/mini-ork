"""mo_emit_hook — Python port of lib/mo_emit_hook.sh.

Faithful port of the internal bash helper ``_mo_emit_hook``: an outbound
event-callback hook that turns mini-ork's DB-only event emission into a
push channel for external observers (dashboards, CI bridges, chat bots).
The hook is a single shell command supplied via the ``MINI_ORK_ON_EVENT``
env var. Mini-ork calls it best-effort after every DB write with three
positional args (event_type, run_id, payload_json); non-zero exit,
timeout, or missing command is logged-and-swallowed so observability
never breaks dispatch.

Co-existence model (strangler-fig): ``lib/mo_emit_hook.sh`` stays
byte-identical. This port gives Python callers an in-process target and
``tests/unit/test_mo_emit_hook_py.py`` a stable surface to diff against
the LIVE bash subprocess.

Behavioural parity (every branch from the bash original is preserved):

  1. ``MINI_ORK_ON_EVENT`` unset or empty → silent no-op (return 0).
  2. Missing positional args default to ``"unknown"`` / ``""`` / ``"{}"``
     to match bash's `${1:-unknown}` / `${2:-}` / `${3:-...}` default
     expressions. The literal payload default is the two-byte string
     ``{}`` — NOT ``json.dumps({})`` (same bytes, different meaning).
  3. Relative ``.sh`` path is resolved to absolute via the bash regex
     anchored at first-char-not-slash, ending in literal ``.sh``. If the
     file exists, it is rewritten as ``$(cd dirname && pwd)/basename``;
     non-relative paths and non-``.sh`` paths pass through unchanged.
  4. Timeout binary is probed in bash's priority order:
     ``gtimeout`` (GNU coreutils on macOS) preferred, ``timeout`` as
     fallback. If neither is on PATH, the hook runs unguarded — matches
     bash's two-branch ``if [ -n "$timeout_bin" ]`` decision.
  5. Hook invocation is hard-capped at 5 seconds; stdout and stderr are
     redirected to ``DEVNULL``; non-zero exit codes are swallowed
     (``|| true`` in bash; bare ``subprocess.run`` in Python — it
     already raises only on timeout, which the caller catches).
  6. ``shell=False`` so the user's chosen binary wins, matching bash's
     direct ``exec`` (no PATH re-interpretation, no shell-quoting bugs).

Public surface:
    mo_emit_hook(event_type: str = "unknown",
                 run_id: str = "",
                 payload_json: str = "{}") -> None

Returns None; never raises on hook failure (best-effort observability).
The sole documented failure mode of the *bash* version is that a missing
``MINI_ORK_ON_EVENT`` is a no-op — this port preserves that contract.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Optional


_HOOK_TIMEOUT_S = 5
_DEFAULT_PAYLOAD = "{}"  # bash ${3:-{\}} → literal '{}'
_DEFAULT_EVENT_TYPE = "unknown"


def _resolve_hook_path(hook: str) -> str:
    """Mirror bash's `[[ "$hook" =~ ^[^/].*\\.sh$ ]]` + file-exists check.

    A relative path ending in ``.sh`` whose file exists is rewritten as
    ``<abspath>/<basename>`` via ``cd dirname && pwd``. Absolute paths
    and non-``.sh`` paths pass through unchanged.
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
    """Mirror bash's `gtimeout` → `timeout` probe order.

    Returns the bare binary name (suitable for ``subprocess.run`` first
    arg) or ``None`` if neither is on PATH. Caller decides whether to
    invoke with the timeout prefix or unguarded.
    """
    if shutil.which("gtimeout") is not None:
        return "gtimeout"
    if shutil.which("timeout") is not None:
        return "timeout"
    return None


def mo_emit_hook(event_type: str = _DEFAULT_EVENT_TYPE,
                 run_id: str = "",
                 payload_json: str = _DEFAULT_PAYLOAD) -> None:
    """Invoke the user's ``MINI_ORK_ON_EVENT`` hook (best-effort).

    Reads ``MINI_ORK_ON_EVENT`` at call time (matches bash), no-ops if
    unset/empty, resolves a relative ``.sh`` path to absolute, probes
    ``gtimeout``/``timeout``, and runs the hook with a 5s hard cap and
    swallowed stdout/stderr/rc. Never raises.
    """
    hook = os.environ.get("MINI_ORK_ON_EVENT", "")
    if not hook:
        return

    hook = _resolve_hook_path(hook)

    timeout_bin = _resolve_timeout_bin()
    if timeout_bin is not None:
        argv = [timeout_bin, str(_HOOK_TIMEOUT_S), hook,
                event_type, run_id, payload_json]
    else:
        argv = [hook, event_type, run_id, payload_json]

    subprocess.run(argv, shell=False, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=False)