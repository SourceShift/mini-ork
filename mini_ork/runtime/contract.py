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
import json
import time
from typing import Mapping, Sequence, Union

__all__ = ["exec_local", "mo_runtime_exec", "run_check"]

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


# Argument accepted by ``run_check``: a list-form argv (preferred, no shell)
# OR a string command run via ``sh -c`` to match ``RemoteWorkspace.exec``'s
# ``argv=[sh,-c,cmd]`` contract on the remote branch. Callers that need
# byte-identical local semantics should pass a list (matches the existing
# ``subprocess.run(argv, …)`` shape used by ``_run_verifier_ref``).
RunArgv = Union[Sequence[str], str]


def _argv_for_local(argv_or_cmd: RunArgv) -> list[str]:
    """Normalize ``argv_or_cmd`` to a list for ``subprocess.run`` on the local branch.

    A list passes through unchanged; a string is run via ``sh -c`` so the
    remote branch (which always wraps with ``sh -c`` on the node-agent) and
    the local branch accept the same input shape.
    """
    if isinstance(argv_or_cmd, (list, tuple)):
        return [str(a) for a in argv_or_cmd]
    return ["sh", "-c", str(argv_or_cmd)]


def _resolve_remote_session(run_id: str, env):
    """Return a remote workspace for ``run_id``, or ``None`` if placement is local.

    Imports are deferred to module-call time so the runtime contract stays
    importable on a frozen build without forking the fast paths. The lookup
    itself is the documented session registry (``get_run_session``); a
    placement other than ``"remote"`` — or no session at all — returns
    ``None`` and the helper takes the local path. ``env`` is the
    dispatch-env snapshot ``get_run_session`` requires; on the local branch
    we pass an empty dict to avoid reading ``os.environ`` (which would
    contradict the contextvar-isolated lookup contract).
    """
    if not run_id or not _placement_is_remote(env or {}):
        return None
    try:
        from mini_ork.runtime.workspace_session import get_run_session
    except Exception:
        return None
    try:
        return get_run_session(run_id, "remote", env=env or {})
    except Exception:
        return None


def _node_interpreter(argv: list[str]) -> list[str]:
    """A check launched with THIS machine's Python (``sys.executable``, e.g. a
    venv under the laptop's checkout) runs under the node's ``python3``: the
    local path does not exist there, and the agent image puts the engine on
    that interpreter's PYTHONPATH."""
    if argv and os.path.isabs(argv[0]) and (
            argv[0] == sys.executable
            or os.path.realpath(argv[0]) == os.path.realpath(sys.executable)):
        return ["python3", *argv[1:]]
    return argv


def _placement_is_remote(env: Mapping[str, str]) -> bool:
    """Checks go remote exactly when the run's agent spawns do: ``MO_PLACEMENT=
    remote``, or the isolation selector (scope=agent + backend=remote). Without
    this gate every check of every LOCAL run tried to open a remote session —
    and would have run remotely on any host with a node configured."""
    if (env.get("MO_PLACEMENT") or "").strip().lower() == "remote":
        return True
    return ((env.get("MO_SANDBOX_SCOPE") or "").strip() == "agent"
            and (env.get("MO_SANDBOX_BACKEND") or "").strip() == "remote")


def run_check(
    argv_or_cmd: RunArgv,
    *,
    cwd: str,
    env: Mapping[str, str] | None = None,
    evidence_path: str = "",
    timeout: float = 0,
) -> tuple[int, "str | bytes"]:
    """Run a check command at the resolved placement, capturing output to ``evidence_path``.

    Routing helper (kickoff ``remote-nodes-11`` §1): one entry point used by
    every site that **checks** code — verifier refs, post-run verify,
    step_rules git, and the mutation test-command loop. Decision code (JSON
    ``pass`` parsing, the vacuous-pass rule, gate arithmetic) stays at the
    call site and is untouched.

    Local branch (``run_id`` unset or placement != ``remote``) is
    byte-identical to a direct ``subprocess.run`` that writes the merged
    output to ``evidence_path``: argv form passes through, string form is
    wrapped in ``sh -c``, env is layered on top of ``os.environ``, and the
    process runs as a child of the call site (no own pgid, mirroring the
    legacy ``subprocess.run`` shape). No event is emitted from the local
    branch — attempt-1 emitted ``mo_node_emit: run_id required`` to a
    verifier's stderr because the helper tried to emit from the local
    branch when ``run_id`` was unset, and that warning surfaced inside
    the verifier's evidence stream.

    Remote branch resolves a ``RemoteWorkspace`` for the run's
    ``MINI_ORK_RUN_ID`` (contextvar-isolated per Bottleneck #1) and
    delegates to ``RemoteWorkspace.exec`` — the same seam every
    remote-nodes spawn uses, so sync-up + run-dir mirror + exec +
    run-dir pull semantics are inherited. The replica is then snapped to
    the session's ``last_synced`` snapshot via ``restore_replica()`` so
    verifier droppings or mutation residue cannot contaminate the next
    sync-down. ``evidence_path`` is satisfied either by the run-dir
    pull (the normal path) or by writing the merged output of ``exec``
    verbatim as a fallback when the pull missed it.

    Args:
        argv_or_cmd: argv list (preferred) or shell string to run.
        cwd:        working directory for the check. Forwarded to the
                    remote via the run's PathMap (so a ``/workspace/target``
                    cwd reaches the replica correctly) and applied as
                    ``cwd=cwd`` on the local branch.
        env:        environment layered on top of ``os.environ``. ``None``
                    means inherit the parent process env (the legacy
                    ``subprocess.run`` default).
        evidence_path: when given, the merged output of the check is
                    written here as bytes (matches the existing
                    ``_run_verifier_ref`` contract). For the remote branch
                    the run-dir pull is the primary path; this is the
                    fallback when the pull did not deliver the file.
        timeout:    seconds; 0 waits forever. Local branch forwards to
                    ``subprocess.run`` (the legacy shape had no timeout).
                    Remote branch forwards to ``RemoteWorkspace.exec``
                    (server-side timeout).

    Returns:
        ``(rc, evidence)``: ``rc`` is the child process's exit code.
        ``evidence`` is bytes on the local branch (the merged stream was
        written to ``evidence_path``; this slot carries the empty bytes),
        and text on the remote branch (the ``output`` of
        ``RemoteWorkspace.exec``).
    """
    # Resolve placement via the per-run contextvar (Bottleneck #1: env
    # isolation). A legacy CLI invocation that never set MINI_ORK_RUN_ID
    # takes the local branch — never read os.environ for this lookup.
    run_id = ""
    env_snapshot: Mapping[str, str] = {}
    try:
        from mini_ork.context import context_env, context_env_snapshot
        run_id = context_env("MINI_ORK_RUN_ID", "") or ""
        env_snapshot = context_env_snapshot()
    except Exception:
        run_id = ""

    # Layer the caller-supplied env on top of the dispatch snapshot so a
    # remote branch sees the same merged env the local branch would.
    if env:
        merged_dispatch_env: Mapping[str, str] = {**env_snapshot, **{str(k): str(v) for k, v in env.items()}}
    else:
        merged_dispatch_env = env_snapshot

    remote = _resolve_remote_session(run_id, merged_dispatch_env) if run_id else None
    if remote is not None:
        # Compose with the production seam; do NOT duplicate sync-up +
        # mirror_push + exec + mirror_pull — RemoteWorkspace.exec owns
        # the full cycle. ``RemoteWorkspace.exec`` accepts only ``cmd``
        # (string); a list-form argv_or_cmd is joined with ``sh -c``-
        # safe quoting so the server-side argv-only contract is preserved.
        # Translate cwd / argv / env to the sandbox view (D4): a verifier
        # script under the engine checkout, the evidence path and the target
        # are host paths that do not exist on the node.
        from mini_ork.dispatch.providers import _run_path_map
        from mini_ork.runtime.backends._workspace_env import isolated_env

        path_map = _run_path_map(merged_dispatch_env)
        remote_cwd = path_map.path(cwd) if (path_map and cwd) else cwd
        remote_env = None
        if path_map:
            # The allowlist governs the AMBIENT env; what the caller hands this
            # check (plan / artifact paths) is meant for it and passes, translated.
            remote_env = isolated_env(env_snapshot, path_map)
            remote_env.update(path_map.env({str(k): str(v) for k, v in (env or {}).items()}))
            for key in [k for k in remote_env if k.startswith("MO_NODE_TOKEN")]:
                remote_env.pop(key)
        if isinstance(argv_or_cmd, (list, tuple)):
            import shlex
            argv = _node_interpreter([str(a) for a in argv_or_cmd])
            parts = path_map.argv(argv) if path_map else argv
            cmd_str = " ".join(shlex.quote(str(a)) for a in parts)
        else:
            cmd = str(argv_or_cmd)
            if cmd.startswith(sys.executable + " "):
                cmd = "python3" + cmd[len(sys.executable):]
            cmd_str = path_map.text(cmd) if path_map else cmd
        exec_kwargs = {"env": remote_env} if remote_env is not None else {}
        check_started = time.monotonic()
        rc, out = remote.exec(cmd_str, cwd=remote_cwd, timeout=int(timeout or 0), **exec_kwargs)
        # remote-nodes-15: one `remote.check` event per remote check — the
        # run-events proof that a verifier ran on the node, and its rc.
        try:
            from mini_ork.observability.node_events import mo_node_emit

            words = cmd_str.split()
            script = next((w for w in words if w.endswith((".py", ".sh"))), words[0] if words else "")
            mo_node_emit(run_id, "verifier", "verifier", "remote.check", json.dumps(
                {"name": os.path.basename(script), "rc": rc,
                 "ms": int((time.monotonic() - check_started) * 1000)}))
        except Exception:  # noqa: BLE001 — the event is advisory
            pass
        # Replica hygiene (kickoff §4): restore from last_synced after
        # every check so verifier droppings / mutation residue cannot be
        # mistaken for agent edits on the next sync-down. ``restore_replica``
        # is a RemoteWorkspace-only verb (duck-typed via ``getattr`` so the
        # helper stays agnostic of the Workspace Protocol's narrow surface)
        # and is a no-op when no snapshot exists (legacy/unpreced-run session).
        restore = getattr(remote, "restore_replica", None)
        if callable(restore):
            try:
                restore()
            except Exception:
                # Restore failure MUST NOT mask the check's rc — the truth
                # is the check's verdict; replica hygiene is best-effort.
                pass
        # Evidence pull fallback (kickoff §1 last paragraph): the run-dir
        # pull is the primary path; write the merged output of ``exec``
        # verbatim to ``evidence_path`` when the pull missed the file.
        if evidence_path:
            try:
                from pathlib import Path
                p = Path(os.path.abspath(evidence_path))
                if not (p.is_file() and p.stat().st_size > 0):
                    p.parent.mkdir(parents=True, exist_ok=True)
                    data = out.encode("utf-8", "replace") if isinstance(out, str) else (out or b"")
                    p.write_bytes(data)
            except Exception:
                pass
        return rc, out

    # Local branch — byte-identical to the legacy ``subprocess.run`` shape.
    # The local branch MUST stay a plain ``subprocess.run`` (NOT ``Popen``):
    # the legacy call sites already used that shape, and the byte-level
    # parity tests (e.g. ``test_execute_run_verifier_ref_py_dispatch_env_and_rc``
    # that asserts the merged stream is byte-for-byte the legacy output,
    # AND ``assert buf.getvalue() == ""`` for stderr) shift if we change
    # process-grouping. Byte-identical means we use the exact
    # ``subprocess.run`` shape the call sites already used.
    argv = _argv_for_local(argv_or_cmd)
    merged_env = dict(os.environ)
    if env:
        for k, v in env.items():
            merged_env[str(k)] = str(v)
    # Capture stdout via PIPE so the returned ``out`` is the merged bytes
    # (callers write that to ``evidence_path`` themselves). The
    # ``evidence_path`` argument, when given, ALSO receives the same
    # merged bytes — the two writes never disagree, and the vacuous-pass
    # size check downstream keeps working.
    proc = subprocess.run(  # noqa: S603
        argv,
        cwd=cwd or None,
        env=merged_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    out_bytes = proc.stdout or b""
    if evidence_path:
        try:
            from pathlib import Path
            ep = Path(os.path.abspath(evidence_path))
            ep.parent.mkdir(parents=True, exist_ok=True)
            ep.write_bytes(out_bytes)
        except Exception:
            pass
    return (proc.returncode, out_bytes)
