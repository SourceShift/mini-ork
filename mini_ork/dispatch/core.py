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
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
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
    try:
        proc = subprocess.Popen(
            list(argv),
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
            _terminate_process_group(proc)
            proc.wait()
            for reader in readers:
                reader.join(timeout=10)
            return 124, "", f"timeout after {timeout}s"

        # Joining without a deadline matches communicate()'s drain semantics: the
        # child is reaped, so this returns as soon as the pipes close.
        for reader in readers:
            reader.join()

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
    assert no host prefixes survive. Under the ``remote`` backend a leak
    raises (``UnmappedHostPathError``); under ``docker``/``local``/
    ``microvm`` a leak logs a WARN per value so existing docker users stay
    unbroken. The map applies here, and ONLY here, so the on-disk record in
    ``runs/<id>/agent-<node>.prompt.json`` stays host-readable.
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
