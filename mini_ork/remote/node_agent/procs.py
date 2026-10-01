"""Detached-process registry for the node-agent (kickoff requirement 3).

This is the data-plane's process supervisor: it spawns long-running
processes inside a session container (or directly on the host under the
``host`` runtime), captures stdout/stderr to per-pid files, persists a
minimal ``<pid>.json`` so the registry survives a node-agent restart, and
exposes byte-offset streaming so a re-connecting client never loses or
duplicates bytes.

Process-group discipline mirrors :func:`mini_ork.dispatch.core.spawn_local`:
the host-side ``Popen`` uses ``start_new_session=True`` so ``killpg``
reaches the whole tree on timeout (rc=124) or kill. The ``host`` runtime
spawns the argv directly; the ``docker`` runtime spawns ``docker exec -i
<cid> setsid <argv>`` so the container-side process group is also its own.

Env KEYS ONLY are persisted to ``<pid>.json``. Values resolve at exec
time from the parent process's ``os.environ``; D6 from
``docs/architecture/remote-nodes.md`` is the contract.
"""
from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping


# ----- constants ------------------------------------------------------------

_PROCS_DIR = ".procs"
# Sentinel RCs match ``core.dispatch`` (``spawn_local``); the kickoff
# explicitly cites them on lines 55-56.
_RC_TIMEOUT = 124
_RC_SPAWN_FAILED = 127

# Mirror ``mini_ork.runtime.backends.docker._RUN_LABEL`` so the reaper
# (separate from this module) can sweep leaked session containers.
# Kept as a string constant for future reaper hooks; the runtime
# session manager reads its own labels directly.
_RUN_LABEL = "mo.sandbox=1"


# ----- types ---------------------------------------------------------------


@dataclass
class ProcSpec:
    """What the caller asked us to run."""

    argv: list[str]
    env_keys: list[str]
    cwd: str | None
    stdin: str
    timeout_s: float | None
    # Values the client sent for this proc (lane secrets, the run contract).
    # In memory only: persistence records the KEYS (D6), never these values.
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class _ProcState:
    pid: int
    run_id: str
    spec: ProcSpec
    state: str = "starting"  # starting | running | exited | killed | orphaned | spawn_failed
    rc: int | None = None
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    out_offset: int = 0
    err_offset: int = 0
    os_pid: int | None = None  # the host-side Popen pid — what restart rediscovery checks
    proc: subprocess.Popen | None = None  # only while running; not persisted


# ----- helpers --------------------------------------------------------------


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    """Single test seam for *binary* CLI calls (docker, git bundle, …).

    Mirrors :func:`mini_ork.runtime.sandbox_reaper._run` so a TestClient
    can monkeypatch one function and drive the whole registry without a
    real daemon. The *process* itself (the detached child) does NOT flow
    through here — it uses :class:`subprocess.Popen` directly so we own
    its lifetime. Tests inject a fake ``_spawn`` if they need to skip
    even that.
    """
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
    )


def _default_spawn(argv: list[str], *, stdin: str, env: Mapping[str, str],
                   cwd: str | None) -> subprocess.Popen:
    """Default Popen for the detached child — mirrors ``spawn_local``.

    ``stdin`` is written immediately so prompts that need it land before
    the child blocks on read; mirrors the discipline in
    ``mini_ork.dispatch.core.spawn_local``.
    """
    proc = subprocess.Popen(
        list(argv),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        env=dict(env),
        cwd=cwd,
        start_new_session=True,
    )
    if stdin:
        try:
            stdin_pipe = proc.stdin
            if stdin_pipe is not None:
                stdin_pipe.write(stdin)
                stdin_pipe.flush()
        except (BrokenPipeError, OSError):
            pass
    return proc


def _resolve_env(env_keys: Iterable[str]) -> dict[str, str]:
    """Resolve env values from the current process env. Empty string when unset.

    Always carries the host's ``PATH`` so the host-side ``docker exec``
    Popen (and any other CLI invocation) can find its binary. The
    in-container ``PATH`` is set by the image's ENV; we never try to
    rewrite it from here.
    """
    env = {"PATH": os.environ.get("PATH", "")}
    for k in env_keys:
        env[k] = os.environ.get(k, "")
    return env


# ----- registry -------------------------------------------------------------


class ProcRegistry:
    """In-memory + disk-backed registry of detached procs for ONE session.

    On construction it rebuilds from ``<state>/runs/<run_id>/.procs/*.json``
    so a node-agent restart re-discovers still-running children by pid
    + container; orphan state is recorded (never silently re-attached).
    """

    def __init__(
        self,
        state_dir: Path,
        run_id: str,
        *,
        runtime: str = "docker",
        session_cid: str | None = None,
        _run_fn: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
        _spawn_fn: Callable[..., subprocess.Popen] | None = None,
    ) -> None:
        self.state_dir = Path(state_dir) / "runs" / run_id / _PROCS_DIR
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.runtime = runtime
        self.session_cid = session_cid
        self._run = _run_fn or _run
        self._spawn = _spawn_fn or _default_spawn
        self._procs: dict[int, _ProcState] = {}
        self._lock = threading.Lock()
        self._pid_seq = self._scan_pids_on_disk()
        self._rebuild_from_disk()

    # ---- disk paths ----------------------------------------------------

    def _proc_files(self, pid: int) -> tuple[Path, Path, Path]:
        d = self.state_dir
        return d / f"{pid}.json", d / f"{pid}.out", d / f"{pid}.err"

    # ---- restart rebuild -----------------------------------------------

    def _scan_pids_on_disk(self) -> int:
        max_pid = 0
        for j in self.state_dir.glob("*.json"):
            try:
                max_pid = max(max_pid, int(j.stem))
            except ValueError:
                continue
        return max_pid

    def _rebuild_from_disk(self) -> None:
        """Reload <pid>.json entries; mark orphaned when the child is gone."""
        for j in sorted(self.state_dir.glob("*.json")):
            try:
                pid = int(j.stem)
                data = json.loads(j.read_text(encoding="utf-8"))
            except (ValueError, OSError, json.JSONDecodeError):
                continue
            state = data.get("state", "exited")
            os_pid = data.get("os_pid")
            if state == "running":
                # Re-discover the child by its OS pid (``pid`` is only this
                # registry's sequence number). Alive -> running, else orphaned.
                alive = bool(os_pid) and self._pid_alive(int(os_pid))
                state = "running" if alive else "orphaned"
            spec = ProcSpec(
                argv=data.get("argv", []),
                env_keys=data.get("env_keys", []),
                cwd=data.get("cwd"),
                stdin=data.get("stdin", ""),
                timeout_s=data.get("timeout_s"),
            )
            ps = _ProcState(
                pid=pid,
                run_id=self.run_id,
                spec=spec,
                state=state,
                rc=data.get("rc"),
                started_at=data.get("started_at", time.time()),
                ended_at=data.get("ended_at"),
                out_offset=data.get("out_offset", 0),
                err_offset=data.get("err_offset", 0),
                os_pid=os_pid,
            )
            self._procs[pid] = ps

    def _pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    # ---- persistence ----------------------------------------------------

    def _persist(self, ps: _ProcState) -> None:
        j, _, _ = self._proc_files(ps.pid)
        # env_keys only; values are NEVER on disk (D6 contract).
        payload = {
            "pid": ps.pid,
            "run_id": ps.run_id,
            "argv": ps.spec.argv,
            "env_keys": sorted(set(ps.spec.env_keys) | set(ps.spec.env)),
            "os_pid": ps.os_pid,
            "cwd": ps.spec.cwd,
            "stdin": ps.spec.stdin,
            "timeout_s": ps.spec.timeout_s,
            "state": ps.state,
            "rc": ps.rc,
            "started_at": ps.started_at,
            "ended_at": ps.ended_at,
            "out_offset": ps.out_offset,
            "err_offset": ps.err_offset,
        }
        tmp = j.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, j)

    # ---- spawn ---------------------------------------------------------

    def _next_pid(self) -> int:
        self._pid_seq += 1
        return self._pid_seq

    def _build_host_argv(self, spec: ProcSpec) -> list[str]:
        """Compose the argv that lands the child in the right process group."""
        if self.runtime == "host":
            return list(spec.argv)
        # docker runtime — the host-side Popen runs `docker exec -i` so the
        # in-container process group is a fresh pgid (setsid inside the
        # container keeps the discipline). The `-i` keeps stdin open.
        assert self.session_cid, "docker runtime requires a session container"
        # `docker exec` forwards no environment of its own: each variable needs
        # `-e KEY`. Bare `-e KEY` reads the value from the docker CLI's env, so
        # secrets never appear in argv (or `ps`). The working dir is inside the
        # container (-w), not the host Popen's cwd.
        env_flags: list[str] = []
        for key in sorted(spec.env):
            env_flags += ["-e", key]
        return [
            "docker", "exec", "-i", "-w", spec.cwd or "/workspace/target",
            *env_flags, self.session_cid, "setsid",
            *spec.argv,
        ]

    def _host_cwd(self, cwd: str | None) -> str | None:
        """The host-side Popen cwd. Docker runtime: none (the container's -w
        applies). Host runtime: /workspace/<x> maps onto this run's <x> dir."""
        if self.runtime != "host" or not cwd:
            return None
        if cwd == "/workspace" or cwd.startswith("/workspace/"):
            rel = cwd[len("/workspace"):].lstrip("/")
            return str(self.state_dir.parent / rel) if rel else str(self.state_dir.parent)
        return cwd

    def spawn(self, spec: ProcSpec) -> _ProcState:
        pid = self._next_pid()
        ps = _ProcState(pid=pid, run_id=self.run_id, spec=spec)
        _, out_path, err_path = self._proc_files(pid)
        out_path.touch()
        err_path.touch()

        env = {"PATH": os.environ.get("PATH", ""), **spec.env}
        argv = self._build_host_argv(spec)

        try:
            proc = self._spawn(argv, stdin=spec.stdin, env=env, cwd=self._host_cwd(spec.cwd))
        except (OSError, FileNotFoundError):
            ps.state = "spawn_failed"
            ps.rc = _RC_SPAWN_FAILED
            ps.ended_at = time.time()
            self._procs[pid] = ps
            self._persist(ps)
            return ps
        ps.proc = proc
        ps.os_pid = getattr(proc, "pid", None)
        ps.state = "running"
        self._procs[pid] = ps
        self._persist(ps)
        # Drain stdout/stderr in background threads, append to disk, and
        # watch for timeout + exit. On exit we mark ``exited`` (or
        # ``killed``) and persist.
        t = threading.Thread(
            target=self._watch_proc,
            args=(ps, proc, out_path, err_path),
            daemon=True,
        )
        t.start()
        return ps

    def _watch_proc(
        self,
        ps: _ProcState,
        proc: subprocess.Popen,
        out_path: Path,
        err_path: Path,
    ) -> None:
        out_f = out_path.open("ab", buffering=0)
        err_f = err_path.open("ab", buffering=0)
        timeout = ps.spec.timeout_s
        deadline = None if timeout is None else time.time() + float(timeout)
        # Read in binary mode to avoid the text-mode ``read()`` blocking
        # indefinitely when the child produces no output (the sleep case
        # in the timeout tests). We pull bytes and decode on append.
        timed_out = False
        try:
            while True:
                # Poll exit; if exited, drain whatever's left and break.
                exited = proc.poll() is not None
                # Non-blocking read of both pipes.
                for stream, sink in ((proc.stdout, out_f), (proc.stderr, err_f)):
                    if stream is None:
                        continue
                    fd = stream.fileno()
                    rlist, _, _ = select.select([fd], [], [], 0.05)
                    if not rlist:
                        continue
                    chunk = os.read(fd, 4096)
                    if chunk:
                        sink.write(chunk)
                ps.out_offset = out_path.stat().st_size
                ps.err_offset = err_path.stat().st_size
                if exited:
                    break
                if deadline is not None and time.time() >= deadline:
                    # A SIGKILL'd child returns a negative rc (-9). Per the
                    # A.1 process contract (kickoff line 56), timeout MUST
                    # surface as rc=124 so dispatch_with_fallback abandons
                    # this lane; a plain ``killed`` writes the OS rc.
                    self._kill_group(proc, ps)
                    proc.wait()
                    timed_out = True
                    break
        finally:
            try:
                out_f.close()
            except OSError:
                pass
            try:
                err_f.close()
            except OSError:
                pass

        # Persist the final state once both files are closed. Using a
        # separate block keeps the watcher loop above free of locking
        # for the common-case ``exited`` path.
        if timed_out:
            rc = _RC_TIMEOUT
            final_state = "timeout"
        else:
            rc = proc.returncode if proc.returncode is not None else _RC_SPAWN_FAILED
            final_state = "killed" if ps.state == "killed" else "exited"
        with self._lock:
            ps.proc = None
            ps.rc = rc
            ps.ended_at = time.time()
            ps.state = final_state
            ps.out_offset = out_path.stat().st_size
            ps.err_offset = err_path.stat().st_size
            self._persist(ps)

    @staticmethod
    def _kill_group(proc: subprocess.Popen, ps: _ProcState) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass
        ps.state = "killed"

    # ---- kill ----------------------------------------------------------

    def kill(self, pid: int) -> bool:
        ps = self._procs.get(pid)
        if ps is None or ps.proc is None:
            return False
        self._kill_group(ps.proc, ps)
        return True

    # ---- read ----------------------------------------------------------

    def get(self, pid: int) -> _ProcState | None:
        return self._procs.get(pid)

    def read_chunk(self, pid: int, *, stream: str, offset: int) -> bytes | None:
        ps = self._procs.get(pid)
        if ps is None:
            return None
        path = self._proc_files(pid)[1 if stream == "out" else 2]
        try:
            size = path.stat().st_size
        except OSError:
            return None
        if offset >= size:
            return b""
        with path.open("rb") as f:
            f.seek(offset)
            return f.read()


__all__ = [
    "ProcRegistry",
    "ProcSpec",
    "_RC_TIMEOUT",
    "_RC_SPAWN_FAILED",
]