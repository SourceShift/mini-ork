"""Mid-node operator-steering injector — Python port of lib/mid_node_injector.sh.

Faithful port of the deterministic sidecar surfaces of
``lib/mid_node_injector.sh``: the two pure formatters and the per-tick
DB→fifo/prompt pipeline that an in-process caller would invoke. The bash
script stays in place (strangler-fig co-existence) so the production
sidecar keeps working; this module gives Python callers an in-process
target and gives ``tests/unit/test_mid_node_injector_py.py`` a stable
surface to byte-diff against the live bash subprocess.

Co-existence model (strangler-fig): bash ``lib/mid_node_injector.sh`` is
the authoritative source. Parity is enforced by the live-subprocess
harness in ``tests/unit/test_mid_node_injector_py.py`` (>=6 cases; floats
1e-6; DB row-by-row diff via sqlite3 SELECT on operator_steering).

JSON encoding choice (note for reviewers): bash uses ``jq -nc --arg t
"$body" '{...}'`` which produces compact (no-whitespace) ASCII-compatible
JSON with jq's native string escaping (Unicode surrogate pairs preserved).
Python mirrors this with ``json.dumps(..., separators=(',', ':'),
ensure_ascii=False)`` — the closest semantic match. Edge-case parity
test (case 3) exercises Unicode + quotes + newlines so any future drift
between jq and json.dumps surfaces immediately.

Scope (parity surface, NOT full bash):
  - The two pure formatters (``_mid_injector_format_claude_user_msg`` /
    ``_mid_injector_format_codex_prompt``) — full parity required.
  - The DB→fifo and DB→codex-fork.out per-tick pipeline (one iteration
    of ``_mid_injector_claude_loop`` / ``_mid_injector_codex_loop``) —
    full parity required; consumers of these functions must have a
    reader pre-attached to the fifo to avoid EPIPE (mirrors bash's
    ``2>/dev/null || true`` on ``printf '%s\\n' "$line" >"$fifo_in"``).
  - The in-process start_claude/stop loop wrapper — Python uses a
    daemon thread + ``threading.Event`` to mirror bash's sidecar
    subprocess; this is a strangler-fig divergence and is NOT covered
    by parity tests (loop wrapper parity is intentionally out of scope,
    matching how operator_steering.py isolates the deterministic SQL
    surface).

Out of scope (intentionally NOT ported — bash keeps owning these):
  - ``kill -0 / kill -TERM`` parent-process signalling — Python loop
    wrapper checks ``os.kill(parent_pid, 0)`` for parity with the
    shell-side ``kill -0`` semantics, but does not actually SIGTERM
    any process.
  - ``codex fork <session_id> "..." --output-format text`` subprocess
    exec — codex_tick appends the formatted prompts to
    ``{fork_out_dir}/codex-fork.out`` as placeholders (no real exec).

Pipeline map (bash function → Python):
  _mid_injector_pid_file               → _resolve_pid_path
  _mid_injector_format_claude_user_msg → format_claude_user_msg
  _mid_injector_format_codex_prompt    → format_codex_prompt
  _mid_injector_claude_loop (one tick) → claude_tick
  _mid_injector_codex_loop  (one tick) → codex_tick
  mid_node_injector_start (claude)     → start_claude
  mid_node_injector_stop               → stop
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

from mini_ork.steering.operator_steering import fetch_for as _steer_fetch

__all__ = [
    "format_claude_user_msg",
    "format_codex_prompt",
    "claude_tick",
    "codex_tick",
    "start_claude",
    "stop",
]


def _resolve_pid_path() -> str:
    """Mirror ``_mid_injector_pid_file`` in lib/mid_node_injector.sh.

    Bash: ``echo "${MINI_ORK_RUN_DIR:-/tmp}/.mid-node-injector.pid"``.
    The /tmp fallback rarely fires in production but is preserved for
    parity so tests that omit MINI_ORK_RUN_DIR exercise the same path.
    """
    base = os.environ.get("MINI_ORK_RUN_DIR") or "/tmp"
    return os.path.join(base, ".mid-node-injector.pid")


def format_claude_user_msg(message: str, severity: str = "info",
                           source: str = "operator") -> str:
    """Mirror ``_mid_injector_format_claude_user_msg`` in lib/mid_node_injector.sh.

    Build a claude-shaped stream-json user message from a steering row.
    Returns one JSON line ready to push into a stream-json fifo.

    Encoding parity: bash uses ``jq -nc --arg t "$body" '{...}'`` —
    compact JSON, no whitespace, jq-native string escaping. Python
    mirrors with ``json.dumps(..., separators=(',', ':'),
    ensure_ascii=False)``. Tested byte-for-byte against the live bash
    subprocess in tests/unit/test_mid_node_injector_py.py.
    """
    body = f"OPERATOR STEERING [{severity}] (from {source}): {message}"
    return json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": body}],
            },
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )


def format_codex_prompt(message: str, severity: str = "info",
                        source: str = "operator") -> str:
    """Mirror ``_mid_injector_format_codex_prompt`` in lib/mid_node_injector.sh.

    Build the codex-shaped continuation prompt from a steering row.
    Returns a plain text string to pass as ``codex fork <id> "..."``.
    Bash uses ``printf '\\n'`` so the newline separator is a literal
    ``\\n`` byte — Python mirrors exactly.
    """
    return (
        f"OPERATOR STEERING [{severity}] (from {source}): {message}\n"
        f"Continue your task with this guidance."
    )


def claude_tick(fifo_in: str, run_id: str, role: str = "any") -> int:
    """Mirror one iteration of ``_mid_injector_claude_loop``.

    Fetch all unconsumed steering rows for (run_id, role) via
    ``operator_steering.fetch_for`` (the single SQL source of truth,
    shared with operator_steering.py) and push each as a formatted
    stream-json user message into ``fifo_in`` followed by ``\\n``.

    Returns the count of rows successfully written. Best-effort: a
    BrokenPipeError or OSError on the write is silently swallowed to
    mirror the bash ``2>/dev/null || true`` after ``printf '%s\\n'
    "$line" >"$fifo_in"`` — when the reader (claude) has closed the
    fifo, the bash printf silently drops the message, and so does this
    function.

    The fetch_for call is one statement that both reads and marks
    consumed, so a second call with the same args on the same DB
    returns 0 (mirrors bash's one-shot fetch + UPDATE).
    """
    rows = _steer_fetch(run_id, role)
    written = 0
    for row in rows:
        line = format_claude_user_msg(
            row["message"],
            row.get("severity") or "info",
            row.get("source") or "operator",
        )
        try:
            with open(fifo_in, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            written += 1
        except (BrokenPipeError, OSError):
            # Mirror bash `2>/dev/null || true` on the printf — best-effort.
            continue
    return written


def codex_tick(session_id_file: str, run_id: str, role: str,
               fork_out_dir: str) -> dict:
    """Mirror one iteration of ``_mid_injector_codex_loop``.

    Fetch unconsumed steering rows for (run_id, role), format each as
    a codex continuation prompt, and append the prompts to
    ``{fork_out_dir}/codex-fork.out`` (placeholder text — bash invokes
    ``codex fork`` for real; Python only writes the prompt text, no
    exec).

    Returns ``{'written': n, 'session_id': sid, 'prompts': [...]}`` or
    ``{'written': 0, 'reason': 'no_session_id'}`` when the session_id
    file is empty (mirrors bash's "dropped steering — session_id not
    yet captured" stderr + continue, which is the permanent-loss
    warning at lib/mid_node_injector.sh:131).
    """
    sid = ""
    if os.path.isfile(session_id_file):
        try:
            with open(session_id_file, encoding="utf-8") as f:
                sid = f.read().strip()
        except OSError:
            sid = ""
    if not sid:
        return {"written": 0, "reason": "no_session_id"}

    rows = _steer_fetch(run_id, role)
    if not rows:
        return {"written": 0, "session_id": sid, "prompts": []}

    os.makedirs(fork_out_dir, exist_ok=True)
    out_path = os.path.join(fork_out_dir, "codex-fork.out")
    prompts: list[str] = []
    for row in rows:
        prompt = format_codex_prompt(
            row["message"],
            row.get("severity") or "info",
            row.get("source") or "operator",
        )
        prompts.append(prompt)
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(prompt + "\n")
    return {"written": len(prompts), "session_id": sid, "prompts": prompts}


def start_claude(fifo_in: str, run_id: str, role: str,
                 poll_secs: float = 5.0,
                 parent_pid: int | None = None) -> int:
    """Mirror ``mid_node_injector_start claude …`` in lib/mid_node_injector.sh.

    Spawn a daemon thread that runs ``claude_tick`` in a loop until the
    parent process exits (detected via ``os.kill(parent_pid, 0)``) or
    ``stop()`` flips the threading.Event.

    Returns the thread id (used as the pseudo-PID by stop()). Writes
    the id to the pid file at ``_resolve_pid_path()`` so stop() can
    find it. Defaults ``parent_pid`` to ``os.getpid()`` so a Python
    process can spin up its own sidecar without explicitly passing its
    own PID (mirrors bash's ``$$`` self-reference at
    lib/mid_node_injector.sh:168).

    Strangler-fig divergence: bash uses an out-of-process subprocess
    that other tooling can ``kill -TERM``; Python uses an in-process
    daemon thread plus a threading.Event. Loop wrapper parity is
    intentionally out of scope per the kickoff — parity tests cover
    the deterministic ``claude_tick`` body, not the loop wrapper.
    """
    pid_path = _resolve_pid_path()
    stop_event = threading.Event()
    ppid = parent_pid if parent_pid is not None else os.getpid()

    def _loop() -> None:
        while not stop_event.is_set():
            try:
                os.kill(ppid, 0)
            except ProcessLookupError:
                break
            except PermissionError:
                # Process exists but we lack perms — treat as still alive.
                pass
            try:
                claude_tick(fifo_in, run_id, role)
            except sqlite3.Error:
                # Let DB errors propagate to the test harness; production
                # callers can wrap. Mirrors bash's `2>/dev/null || continue`
                # for the fetch — but a sqlite error mid-tick is loud.
                raise
            if stop_event.wait(poll_secs):
                break

    t = threading.Thread(target=_loop, name="mid-injector-claude", daemon=True)
    t.start()

    # Persist the pseudo-PID so stop() can find the thread + flag it.
    os.makedirs(os.path.dirname(pid_path) or ".", exist_ok=True)
    with open(pid_path, "w", encoding="utf-8") as f:
        # We can't write the thread id (not an int PID); write a sentinel
        # that stop() recognises, paired with the in-process Event.
        f.write(f"thread:{t.ident}\n")
        f.write(f"started:{int(time.time())}\n")
    # Stash the event on a module-level singleton so stop() can reach it
    # without re-reading the file. Module-level state is intentional and
    # limited to this single Event — matches bash's pid-file-as-state.
    global _stop_event
    _stop_event = stop_event
    return int(t.ident or 0)


# Module-level threading.Event used by stop() to signal start_claude's loop.
_stop_event: threading.Event | None = None


def stop(pid_path: str | None = None) -> bool:
    """Mirror ``mid_node_injector_stop`` in lib/mid_node_injector.sh.

    Signal the loop wrapper started by ``start_claude`` to exit, then
    remove the pid file. Returns True if a loop was signalled, False
    otherwise.

    Strangler-fig divergence: bash sends ``kill -TERM`` to a real
    subprocess; Python flips the module-level threading.Event set by
    start_claude. No real signal is sent.
    """
    path = pid_path or _resolve_pid_path()
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass
    global _stop_event
    if _stop_event is not None:
        _stop_event.set()
        _stop_event = None
        return True
    return False