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
     timeout SIGKILLs the whole detached process *group* — and then the ppid
     tree, since the harness starts each of its own commands in a separate
     group — so a hung harness can't orphan grandchildren that keep burning the
     lane (and can't deadlock the output drain by holding the inherited stdout
     pipe).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TextIO

from ..context import context_env_snapshot
from .live_stream import LiveWriter, open_live_writer
from .models import DispatchRequest, DispatchResult, TokenUsage

# Parser callables turn a provider's stdout into structured telemetry. They are
# injected (not hard-wired) so the core stays provider-agnostic and unit-testable
# with a stub provider.
UsageParser = Callable[[str], TokenUsage]
CostParser = Callable[[str, TokenUsage], float]
TextParser = Callable[[str], str]

# Cap on the recorded error text (llm_calls.error_message / DispatchResult.error).
_ERROR_CAP = 2000

# Run-local bookkeeping of the live agent process groups (RBF-25). While a lane
# runs, one ``"<pgid> <unix start ts>\n"`` line lives in this file. The run
# reaper reads it to stop the agents of a dispatcher that died without running
# its teardown — agents run detached in their own session, so they outlive the
# dispatcher otherwise. A line is removed again when the group is reaped
# normally; a stale line therefore means the dispatcher never got to clean up.
AGENT_PGIDS_FILE = ".agent-pgids"


def _agent_pgids_path(env: Mapping[str, str]) -> Path | None:
    """``$MINI_ORK_RUN_DIR/.agent-pgids`` when the run dir is in the child env.

    Only the environment handed to the child is consulted: this is the run
    identity the caller chose for the spawn, and a direct ``spawn_local`` call
    with an explicit env must not adopt an ambient run from the process env.
    """
    run_dir = env.get("MINI_ORK_RUN_DIR") or ""
    return Path(run_dir) / AGENT_PGIDS_FILE if run_dir else None


def _lock_pgids(fh: TextIO) -> None:
    """Serialise the read-modify-write against concurrent lane spawns.

    Best-effort: platforms without ``fcntl`` still append correctly (a single
    O_APPEND write is atomic); only the removal pass loses its interlock.
    """
    try:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except (ImportError, OSError):
        pass


def _record_agent_pgid(path: Path, pgid: int, started_at: int) -> bool:
    """Append the live group's record. Returns False when it could not be written.

    Bookkeeping must never break the spawn: an unwritable run dir degrades to
    the pre-RBF-25 behavior (the reaper cannot stop this group).
    """
    try:
        with path.open("a+", encoding="utf-8") as fh:
            _lock_pgids(fh)
            fh.write(f"{pgid} {started_at}\n")
        return True
    except OSError:
        return False


def _forget_agent_pgid(path: Path, pgid: int) -> None:
    """Remove this group's line after a normal reap, keeping other lanes' lines.

    Read-modify-write under the same lock as the append, so a concurrent lane
    finishing (or starting) in this process cannot have its record dropped.
    """
    try:
        with path.open("a+", encoding="utf-8") as fh:
            _lock_pgids(fh)
            fh.seek(0)
            lines = fh.readlines()
            kept = [line for line in lines if line.split()[:1] != [str(pgid)]]
            if len(kept) != len(lines):
                fh.seek(0)
                fh.truncate()
                fh.writelines(kept)
    except OSError:
        pass


# A record in ``.agent-pgids`` must never outlive the group it names: the run
# reaper decides from a *live* leader, so a pgid that is already gone can only
# mislead it (or, once recycled, point it at an unrelated group). The record is
# removed only after the spawn has been drained, which is strictly after the
# leader exits — a window in which the group would otherwise vanish while the
# line is still readable. A keeper in the leader's session closes that window:
# it polls the leader cheaply and lingers a moment after it goes away. It is
# backgrounded *before* ``exec``, so the agent stays the session/group leader
# and keeps its pid, rc and streams; its stdio is /dev/null so it can never
# hold the drain pipes open.
_GROUP_KEEPER_POLL_S = 0.2
_GROUP_KEEPER_LINGER_S = 0.5
_GROUP_KEEPER_SHIM = (
    f'( l=$$; while kill -0 "$l" 2>/dev/null; do sleep {_GROUP_KEEPER_POLL_S}; done; '
    f'sleep {_GROUP_KEEPER_LINGER_S} ) </dev/null >/dev/null 2>&1 &\n'
    'exec "$@"'
)


def _executable_exists(exe: str, env: Mapping[str, str], cwd: str | None) -> bool:
    """Whether ``env``/``cwd`` would let ``execve`` find ``exe``.

    Only used to decide whether wrapping in the keeper shim is safe: a target
    that does not exist must still reach ``Popen`` unwrapped, so the documented
    ``(127, "", "spawn failed: ...")`` contract is preserved rather than being
    replaced by the shell's own "not found" message.
    """
    if "/" in exe:
        target = exe if os.path.isabs(exe) or cwd is None else os.path.join(cwd, exe)
        return os.access(target, os.X_OK)
    return shutil.which(exe, path=env.get("PATH") or os.defpath) is not None


def _spawn_argv(argv: Sequence[str], env: Mapping[str, str], cwd: str | None,
                *, keep_group: bool) -> list[str]:
    """``argv`` itself, or the keeper shim around it for a recorded group."""
    if not keep_group or not argv or not _executable_exists(argv[0], env, cwd):
        return list(argv)
    # ``$0`` is a placeholder so ``"$@"`` is exactly the caller's argv.
    return ["/bin/sh", "-c", _GROUP_KEEPER_SHIM, "mini-ork-agent", *argv]


# Advisory chatter the Claude CLI prints to stderr at startup, before it does any
# work: the connectors notice on any lane that sets an ANTHROPIC_* auth source
# (which takes precedence over a claude.ai login), and the model-registry warning
# on any lane pinned to a gateway model id the CLI does not ship — the whole
# anthropic-compat family (deepseek, glm, kimi, minimax) trips the second one.
#
# Both are benign and both are the FIRST thing a live view of a lane sees, so
# without this the node inspector shows two warnings where the agent's output
# belongs until the stdout envelope arrives. Matched by substring because the
# connectors notice is prefixed with a "⚠ " glyph on some versions.
_HARNESS_STARTUP_NOISE = (
    "claude.ai connectors are disabled",
    "[claude-code:unrecognized_model]",
)


def _is_harness_startup_noise(line: str) -> bool:
    return any(marker in line for marker in _HARNESS_STARTUP_NOISE)


def _failure_detail(stdout: str, stderr: str) -> str:
    """The error to record for a non-zero lane exit.

    A harness CLI in JSON mode (the Claude CLI's ``--output-format json``)
    reports an API failure in its stdout result envelope —
    ``{"is_error": true, "api_error_status": 403, "result": "API Error: 403 …"}``
    — while stderr may carry only an unrelated advisory banner. Recording
    stderr alone buried the real cause (a capped gateway key) behind that
    banner. Lead with the envelope's message when there is one; keep the
    stderr tail after it as supporting context.
    """
    envelope = ""
    try:
        doc = json.loads(stdout.strip() or "null")
    except ValueError:
        doc = None
    if isinstance(doc, dict) and doc.get("is_error"):
        message = str(doc.get("result") or doc.get("error") or "").strip()
        status = doc.get("api_error_status")
        reason = doc.get("terminal_reason")
        tag = " ".join(str(x) for x in (reason, status) if x)
        if message:
            envelope = f"[{tag}] {message}" if tag else message
    tail = (stderr or "").strip()
    if not envelope:
        return tail[-_ERROR_CAP:]
    envelope = envelope[:_ERROR_CAP // 2]
    if not tail:
        return envelope
    sep = "\n--- stderr ---\n"
    room = _ERROR_CAP - len(envelope) - len(sep)
    return envelope + sep + tail[-room:]


def _descendant_pids(root_pid: int) -> list[int]:
    """Every live descendant of ``root_pid``, read from one ``ps`` snapshot.

    The process-group kill below is not sufficient on its own: the Claude CLI
    runs each Bash tool command in its *own* process group, so those commands —
    and everything under them — survive a kill of the leader's group and
    reparent to pid 1. A cargo build the agent started held its build-directory
    lock for 57 minutes after its node timed out for exactly this reason. The
    ppid tree is the only link that still spans those escaped groups, and it is
    only observable while the leader is alive, so this snapshot is taken
    *before* any signal is sent.

    Fail-soft: any error (no ``ps``, a wedged ``ps``, an unparsable line) yields
    ``[]``. A diagnostic probe that could hang or raise would be worse than the
    orphan it is meant to catch. ``root_pid`` itself is never in the result.
    """
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid="],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    children: dict[int, list[int]] = {}
    try:
        for line in out.stdout.splitlines():
            fields = line.split()
            if len(fields) != 2:
                continue
            pid, ppid = int(fields[0]), int(fields[1])
            children.setdefault(ppid, []).append(pid)
    except ValueError:
        return []
    # Breadth-first over the ppid map, guarding against a cycle in a pid map
    # that changed under us (a reparent to 1, a recycled pid).
    descendants: list[int] = []
    seen = {root_pid}
    frontier = [root_pid]
    while frontier:
        following: list[int] = []
        for parent in frontier:
            for child in children.get(parent, []):
                if child in seen:
                    continue
                seen.add(child)
                descendants.append(child)
                following.append(child)
        frontier = following
    return descendants


def _terminate_process_group(proc: "subprocess.Popen[str]") -> None:
    """SIGKILL the child and everything it started, in or out of its group.

    The child was spawned with ``start_new_session=True``, so it leads its own
    process group; killing that *group* — not just the direct child — reaps the
    grandchildren that share it (claude's helpers, codex's sidecar). But a group
    kill alone does not reach everything: the Claude CLI runs each Bash tool
    command in a *separate* process group, so those commands and their
    descendants survive the group kill and reparent to pid 1 — the orphan that
    kept a cargo build-directory lock, and burned CPU and memory, long after its
    node had been abandoned. So the ppid tree is collected first (once the
    leader dies the link to what it started is gone), then the leader's group is
    killed, then each collected descendant's group is swept. That also matters
    for the drain: the escaped groups hold the inherited stdout pipe, and the
    sweep is what finally closes it.

    Best-effort throughout: the group kill falls back to the direct child, and
    every descendant signal swallows a lookup/permission failure. Never signals
    this process's own group (``os.getpgrp()``), the leader's own already-killed
    group, or pid 1.
    """
    # Snapshot before any signal: after the leader dies its children reparent to
    # pid 1 and the ppid tree that identifies them is lost.
    descendants = _descendant_pids(proc.pid)
    leader_pgid: int | None = None
    try:
        leader_pgid = os.getpgid(proc.pid)
        os.killpg(leader_pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    own_pgid = os.getpgrp()
    for pid in descendants:
        if pid <= 1 or pid == os.getpid():
            continue
        try:
            pgid = os.getpgid(pid)
            # The leader's group was already signalled; our own group must never
            # be signalled (a mis-mapped pid must not take the harness down with
            # it), so those fall back to a single-pid kill.
            if pgid not in (leader_pgid, own_pgid, 1):
                os.killpg(pgid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


# Bound on how long the timeout path waits for a killed child's process group to
# be reaped before giving up and returning rc=124 anyway. The unbounded
# ``proc.wait()`` this replaces is what let a lane recorded as "timeout after
# 120.0s" actually run for 2664 s with no record of where the time went.
# Read at call time so an operator (or a test) can shorten it; a malformed value
# falls back to the default rather than crashing the timeout handler.
_DEFAULT_KILL_REAP_TIMEOUT_S = 30.0
# Last-resort wait when even the bounded reap expires (an unkillable/D-state
# child). SIGKILL was already delivered by this point, so this is only courtesy.
_KILL_ESCALATION_WAIT_S = 10.0


def _kill_reap_timeout_s() -> float:
    """The bounded reap timeout (``MO_KILL_REAP_TIMEOUT_S``, default 30 s)."""
    raw = os.environ.get("MO_KILL_REAP_TIMEOUT_S")
    try:
        value = float(raw) if raw is not None else _DEFAULT_KILL_REAP_TIMEOUT_S
    except (TypeError, ValueError):
        value = _DEFAULT_KILL_REAP_TIMEOUT_S
    return max(0.0, value)


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
    ``execve`` yields ``rc=127``. The prompt rides on stdin, never argv/env, so
    it is structurally E2BIG-proof. The separated-stream, stdin-fed shape is
    exactly what ``dispatch`` needs and what ``Workspace.exec`` (merged output,
    no stdin) deliberately cannot provide — which is why isolation of the CLI
    spawn needs this verb, not ``exec``.

    The pipes are drained by reader threads rather than ``communicate()`` so each
    line can be teed to the live sidecar (``MO_LIVE_FILE``) AS IT ARRIVES. The
    rc contract is unchanged and non-negotiable: a timeout still SIGKILLs the
    group and returns ``124``, and the captured stdout/stderr still reach the
    parsers whole, so switching the drain mechanism is invisible to callers.
    """
    # The per-node live sidecar arrives on the REQUEST env (providers sets it for
    # every in-a-run dispatch); the parent owns the writer and the child never
    # sees the variable. Fall back to the process env for direct callers.
    child_env = dict(env)
    live_path = child_env.pop("MO_LIVE_FILE", "") or None
    # Only a spawn that writes a pgid record needs the keeper shim; without a
    # run dir the command is exec'd exactly as the caller passed it.
    pgids_path = _agent_pgids_path(child_env)
    spawn_argv = _spawn_argv(argv, child_env, cwd, keep_group=pgids_path is not None)
    # Wall clock, not monotonic: the operator's 130-2664 s outliers are
    # wall-clock durations, and macOS's monotonic clock stops during sleep.
    spawned_at = time.time()
    try:
        proc = subprocess.Popen(
            spawn_argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # Explicit utf-8 + replace, not locale-dependent text=True: a harness
            # that emits a truncated multibyte sequence (a killed run's last
            # write is a common one) must not crash the drain thread and lose
            # the whole node's output to a UnicodeDecodeError.
            encoding="utf-8",
            errors="replace",
            env=child_env,
            cwd=cwd,  # None = inherit; pinned by the caller's cwd guard
            start_new_session=True,
        )
    except OSError as exc:
        return 127, "", f"spawn failed: {exc}"

    # Record the group so the run reaper can stop it if this dispatcher dies
    # before its teardown (RBF-25). ``start_new_session`` above makes the
    # leader's pid its pgid. Best-effort: a failed write must not fail the lane.
    pgid: int | None = None
    recorded = False
    if pgids_path is not None:
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = None
        if pgid is not None:
            # Post-spawn wall clock: it is >= the child's real start, so the
            # reaper's ``started <= ts + slack`` guard can never skip our own
            # group because a slow ``Popen`` pushed the record earlier than it.
            recorded = _record_agent_pgid(pgids_path, pgid, int(time.time()))

    with open_live_writer(live_path) as live:
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        readers = [
            threading.Thread(
                target=_drain_stream,
                args=(proc.stdout, live, "stdout", stdout_parts),
                daemon=True,
            ),
            threading.Thread(
                target=_drain_stream,
                args=(proc.stderr, live, "stderr", stderr_parts),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()
        # Third thread, not a bare write: see _feed_stdin for why the prompt
        # cannot be written from the thread that owns the timeout.
        threading.Thread(target=_feed_stdin, args=(proc, stdin), daemon=True).start()

        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # A hung harness is the single biggest reliability failure. Reap its
            # whole detached group, then let the readers drain to EOF — killing
            # the group closes the inherited pipes, which is what unblocks them
            # and reaps the zombie. Then surface the distinct rc=124 so
            # dispatch_with_fallback abandons this lane.
            #
            # Every wait below is bounded. The old bare proc.wait() blocked
            # forever, so a stuck child made a "120 s" lane run for thousands of
            # seconds with nothing recording where the time went. The error text
            # now carries the split so the next outlier is attributable:
            # timeout spent before the kill vs. time the kill took to take.
            _terminate_process_group(proc)
            killed_at = time.time()
            try:
                proc.wait(timeout=_kill_reap_timeout_s())
            except subprocess.TimeoutExpired:
                # _terminate_process_group sends SIGKILL only (there is no
                # SIGTERM phase), so a second TimeoutExpired means the child is
                # unkillable, not merely stubborn. Signal the group once more in
                # case a descendant race admitted a new process, then give it
                # one last bounded wait and return anyway — the OS reaps it
                # after we are gone. Never block forever here.
                _terminate_process_group(proc)
                try:
                    proc.wait(timeout=_KILL_ESCALATION_WAIT_S)
                except subprocess.TimeoutExpired:
                    pass
            reaped_at = time.time()
            for reader in readers:
                reader.join(timeout=10)
            # The group is dead (SIGKILL was delivered): drop its bookkeeping
            # line so a later reap does not signal a recycled pid.
            if recorded and pgids_path is not None and pgid is not None:
                _forget_agent_pgid(pgids_path, pgid)
            return 124, "", (
                f"timeout after {timeout}s "
                f"(spawned→kill {killed_at - spawned_at:.0f}s, "
                f"kill→reaped {reaped_at - killed_at:.0f}s)"
            )

        # Joining without a deadline matches communicate()'s drain semantics: the
        # child is reaped, so this returns as soon as the pipes close.
        for reader in readers:
            reader.join()

    # Normal reap: the group is gone, so its record must not outlive it.
    if recorded and pgids_path is not None and pgid is not None:
        _forget_agent_pgid(pgids_path, pgid)

    return proc.returncode, "".join(stdout_parts), "".join(stderr_parts)


def _drain_stream(
    stream: TextIO | None,
    live: LiveWriter,
    name: str,
    sink: list[str],
) -> None:
    """Drain one pipe line by line, teeing each line to the live sink.

    Iterating the stream (rather than ``read(n)``) is what keeps the tee
    record-aligned: text iteration yields only at a newline or EOF, so a sink
    record never carries half a JSON line unless the harness genuinely died
    mid-write — and that trailing fragment is flagged rather than dropped,
    because it is usually the last thing a killed run said.

    The harness's startup chatter is kept in ``sink`` but withheld from the
    live view — see ``_HARNESS_STARTUP_NOISE``. ``sink`` is what a failed lane
    reports through ``_failure_detail``, so diagnostics are unaffected.
    """
    if stream is None:
        return
    try:
        for line in stream:
            sink.append(line)
            if name == "stderr" and _is_harness_startup_noise(line):
                continue
            live.write_line(line, name, partial=not line.endswith("\n"))
    except (OSError, ValueError):
        # The group was killed under us. What was already drained is still a
        # faithful record of the output; the rc from the caller is the real
        # signal, so this must not turn a clean abandon into a crash.
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _feed_stdin(proc: "subprocess.Popen[str]", stdin: str) -> None:
    """Write the prompt, then close the pipe so the child sees EOF.

    On its own thread because a prompt larger than the pipe buffer (~64 KiB)
    blocks the writer until the child reads. Writing it inline would put the
    thread that owns ``proc.wait(timeout=…)`` inside a write that a
    stdin-ignoring child never unblocks — the timeout would lose its authority
    precisely on the hung-lane case it exists for.
    """
    try:
        if proc.stdin is not None:
            proc.stdin.write(stdin)
            proc.stdin.close()
    except (BrokenPipeError, OSError, ValueError):
        # Child exited before reading its prompt; rc already says so.
        pass


# ── prompt-text host paths (remote-nodes-14 / D6, remote-early-death) ───────
#
# argv and env are strings the child RESOLVES, so a host path there must stay
# fatal. Prompt text is prose the child READS — a recipe prompt that names a
# host file outside the run's four roots is a documentation leak, not a broken
# path, and killing the dispatch over it is what ended run-1791911811-5826
# before its node ever booted (48h triage §8.1: the context pack named the
# kickoff file, which sits under no shipped root and so had no translation).

# Sandbox-safe stand-in for such a token. It is an ordinary ``/workspace/…``
# path so the child reads a plausible path, and it carries no host prefix
# component (``/home``, ``/Users``, …) that could re-trip the leak check.
_UNMAPPED_PATH_PLACEHOLDER = "/workspace/unshared-host-path"
# Characters that end a path token in prose: whitespace, quotes, brackets and
# the punctuation that normally follows a path in a sentence.
_PATH_TOKEN_END = frozenset(" \t\r\n\"'`()[]{}<>,;|")
# The same boundary rule the leak check applies (``PathMap._contains_path_token``):
# a prefix is a token only when it starts and ends at one of these.
_PATH_BOUNDARY = ("/", ":", " ", "\t", "\n")


def _is_path_token(value: str, start: int, prefix: str) -> bool:
    end = start + len(prefix)
    preceded = start == 0 or value[start - 1] in _PATH_BOUNDARY
    followed = end == len(value) or value[end] in _PATH_BOUNDARY
    return preceded and followed


def _path_token_spans(value: str, prefix: str) -> list[tuple[int, int]]:
    """``(start, end)`` of every ``prefix`` token in ``value``.

    ``end`` covers the whole path token (up to the next delimiter) so a
    replacement drops the entire path, not just its prefix."""
    spans: list[tuple[int, int]] = []
    idx = value.find(prefix)
    while idx >= 0:
        if _is_path_token(value, idx, prefix):
            end = idx + len(prefix)
            while end < len(value) and value[end] not in _PATH_TOKEN_END:
                end += 1
            spans.append((idx, end))
            idx = value.find(prefix, end)
        else:
            idx = value.find(prefix, idx + 1)
    return spans


def _rewrite_path_tokens(value: str, prefix: str, placeholder: str) -> tuple[str, list[str]]:
    """Replace every ``prefix`` token in ``value`` with ``placeholder``.

    Whole-token replacement is load-bearing: rewriting only the prefix would turn
    ``/Volumes/Users/x`` into ``<placeholder>/Users/x`` and re-leak on the very
    next check. Returns the rewritten text and the tokens it replaced (the WARN
    names them)."""
    spans = _path_token_spans(value, prefix)
    if not spans:
        return value, []
    out, cursor = [], 0
    for start, end in spans:
        out.append(value[cursor:start])
        out.append(placeholder)
        cursor = end
    out.append(value[cursor:])
    return "".join(out), [value[s:e] for s, e in spans]


def _sanitize_prompt_host_paths(text: str, path_map: object) -> str:
    """Rewrite residual *unmapped* host path tokens in prompt text.

    Called only when the leak check has already failed on the text channel. Every
    token under a forbidden prefix the map does NOT know (``DEFAULT_FORBIDDEN``
    minus ``path_map.host_roots()``) is rewritten to
    :data:`_UNMAPPED_PATH_PLACEHOLDER` and one WARN names the tokens and the
    ``text`` channel, so the leak stays visible in the run log.

    A token under a root the map DOES know still raises — that one is a real
    translation bug, and papering over it would hide exactly what the check
    exists for. The caller keeps argv/env fail-closed and re-runs the check on
    the rewritten text, so a rewrite that does not fully clean the prompt is
    fatal rather than merely unhelpful.
    """
    import logging

    from mini_ork.runtime.path_map import DEFAULT_FORBIDDEN, UnmappedHostPathError

    mapped = [root for root in path_map.host_roots() if root]
    for root in mapped:
        if _path_token_spans(text, root):
            raise UnmappedHostPathError("text", text, tuple(mapped))
    cleaned, replaced = text, []
    for prefix in sorted({p for p in DEFAULT_FORBIDDEN if p} - set(mapped),
                         key=len, reverse=True):
        cleaned, tokens = _rewrite_path_tokens(cleaned, prefix, _UNMAPPED_PATH_PLACEHOLDER)
        replaced.extend(tokens)
    if not replaced:
        # Nothing here is ours to rewrite; the caller's check was right.
        raise UnmappedHostPathError("text", text, tuple(mapped))
    logging.getLogger("mini_ork.dispatch").warning(
        "host path in %s survives translation to remote backend: %r; rewrote the "
        "unmapped host path token(s) to %s",
        "text", replaced, _UNMAPPED_PATH_PLACEHOLDER,
    )
    return cleaned


def _spawn_in_workspace(
    backend: str,
    argv: Sequence[str],
    *,
    stdin: str,
    timeout: float,
    env: Mapping[str, str],
    cwd: str | None,
    path_map: "object | None" = None,
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

    ``path_map`` (D4 / remote-nodes-03): when supplied, translate argv, env
    values, cwd, and stdin via the map BEFORE handing to ``ws.spawn``, then
    assert no host prefixes survive. Under the ``remote`` backend a leak on
    argv/env raises (``UnmappedHostPathError``) — the child resolves those, so a
    host path there is fatal — while a leak in the prompt TEXT is sanitized to
    :data:`_UNMAPPED_PATH_PLACEHOLDER` with a WARN
    (:func:`_sanitize_prompt_host_paths`); a text token under a mapped root still
    raises. Under ``docker``/``local``/``microvm`` every leak logs a WARN per
    value so existing docker users stay unbroken. The map applies here, and ONLY
    here, so the on-disk record in ``runs/<id>/agent-<node>.prompt.json`` stays
    host-readable.
    """
    from mini_ork.runtime.agent_workspace import resolve_spawn_workspace
    from mini_ork.runtime.path_map import UnmappedHostPathError
    from mini_ork.runtime.workspace_session import get_run_session

    applied_argv: Sequence[str] = argv
    applied_env: Mapping[str, str] = env
    applied_cwd: str | None = cwd
    applied_stdin: str = stdin
    if path_map is None and backend == "remote":
        # Without the run's pinned roots there is nothing to translate host
        # paths with, and a remote sandbox cannot see a single one of them.
        raise ValueError(
            "remote workspace needs the run's pinned roots (run_profile.json "
            "'roots'); none were found for this dispatch"
        )
    if path_map is not None:
        from mini_ork.runtime.backends._workspace_env import isolated_env
        from mini_ork.runtime.path_map import PathMap

        if isinstance(path_map, PathMap):
            applied_argv = path_map.argv(list(argv))
            # The full sandbox env contract — allowlist, translated values,
            # MINI_ORK_HOME=/workspace/mo-home, no MINI_ORK_DB, MO_REMOTE_NODE=1,
            # no child spawn — not just translated values.
            applied_env = isolated_env(env, path_map)
            if cwd is not None:
                applied_cwd = path_map.path(cwd)
            if isinstance(stdin, str) and stdin:
                applied_stdin = path_map.text(stdin)

            if backend == "remote":
                # remote-nodes-13 (D6): only the secrets the dispatch layer
                # scoped to THIS lane (named in MO_LANE_SECRET_KEYS) cross to
                # the node; every other secret-named key is dropped, so an
                # ambient OPENAI_API_KEY / ANTHROPIC_API_KEY never leaves.
                # A spawn that bypassed dispatch_model carries no names and
                # therefore no secrets (fail closed).
                from mini_ork.remote.secrets_scope import LANE_SECRET_KEYS_ENV, is_secret_name

                keep = {k for k in str(env.get(LANE_SECRET_KEYS_ENV) or "").split(",") if k}
                scoped_env = {k: v for k, v in dict(applied_env).items()
                              if k != LANE_SECRET_KEYS_ENV and (k in keep or not is_secret_name(k))}
                for key in keep:
                    if key not in scoped_env and env.get(key):
                        scoped_env[key] = env[key]
                applied_env = scoped_env
                # A real remote sandbox is expected to have every path
                # translated; a leak means a host prefix slipped through and
                # the child would point at a file that does not exist there.
                # argv/env stay fatal (the child resolves those). PROMPT TEXT is
                # prose: an out-of-root host path quoted inside it is sanitized
                # to a neutral sandbox path with a WARN, instead of ending the
                # dispatch — that failure (run-1791911811-5826) killed the run
                # before its node ever booted. A mapped-root survivor in the
                # text is still a real translation bug and still raises.
                try:
                    path_map.assert_no_host_paths(
                        argv=applied_argv, env=applied_env, text=applied_stdin
                    )
                except UnmappedHostPathError as exc:
                    if exc.channel != "text":
                        raise
                    applied_stdin = _sanitize_prompt_host_paths(applied_stdin, path_map)
                    # Re-check the sanitized text: whatever still leaks has no
                    # safe rewrite and stays fatal.
                    path_map.assert_no_host_paths(
                        argv=applied_argv, env=applied_env, text=applied_stdin
                    )
            else:
                # docker/local/microvm — log + continue so existing users
                # aren't silently broken by the new check. A WARN per leaked
                # value is loud enough to be visible in run logs without
                # aborting the run.
                try:
                    path_map.assert_no_host_paths(
                        argv=applied_argv, env=applied_env, text=applied_stdin
                    )
                except UnmappedHostPathError as exc:
                    import logging

                    logging.getLogger("mini_ork.dispatch").warning(
                        "host path in %s survives translation to %s backend: %r",
                        exc.channel, backend, exc.value,
                    )

    # Run-scoped workspace session (D2 / remote-nodes-02): one Workspace per
    # (run_id, backend) for the entire run, torn down at run end by the executor.
    # ``MINI_ORK_RUN_ID`` is the canonical signal that we are inside a run; an
    # empty run id falls through to the legacy one-shot lifecycle so ad-hoc
    # dispatch + tests keep today's exact behavior.
    # Session identity, the session marker and the live sidecar are HOST-side
    # concerns: read them from the untranslated env. (The translated env maps
    # MINI_ORK_RUN_DIR / MO_LIVE_FILE to sandbox paths that do not exist here —
    # the marker kill_run depends on was being written to /workspace/run.)
    host_env: Mapping[str, str] = env if isinstance(env, Mapping) else {}
    run_id = host_env.get("MINI_ORK_RUN_ID", "")
    live_file_path = host_env.get("MO_LIVE_FILE", "")
    if isinstance(applied_env, dict):
        applied_env.pop("MO_LIVE_FILE", None)   # the child never writes the sidecar
    # Only the remote backend tees a live sidecar; docker/local/microvm spawn()
    # do not take the kwarg (passing it broke them with a TypeError).
    live_kwargs = {"live_file_path": live_file_path} if backend == "remote" else {}
    if run_id:
        ws = get_run_session(run_id, backend, env=host_env)
        # ``live_file_path`` is an OPTIONAL backend kwarg (remote-nodes-09):
        # RemoteWorkspace uses it to tee per-chunk lines to the node's live
        # sidecar; LocalWorkspace ignores it (host path already tees via
        # ``spawn_local``). The Workspace Protocol deliberately doesn't declare
        # the kwarg so 30+ existing callers stay unchanged.
        return ws.spawn(list(applied_argv), stdin=applied_stdin, timeout=timeout,
                        env=applied_env, cwd=applied_cwd, **live_kwargs)
    ws = resolve_spawn_workspace(backend, env=applied_env, cwd=applied_cwd)
    ws.up()
    try:
        return ws.spawn(list(applied_argv), stdin=applied_stdin, timeout=timeout,
                        env=applied_env, cwd=applied_cwd, **live_kwargs)
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
    proc_env = context_env_snapshot()
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
            path_map=getattr(request, "path_map", None),
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
            error=_failure_detail(stdout, stderr or ""),
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
