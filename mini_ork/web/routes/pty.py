"""WebSocket PTY bridge — attach the UI to a project's orchestrator.

A terminal in a browser is just bytes over a socket: the server owns a real
pseudo-terminal (via ``os.forkpty``), the browser runs xterm.js, and this
endpoint copies bytes both ways. Window resizes travel as an out-of-band
control frame.

Unlike a bare forkpty (where the shell was a child of the request and died on
disconnect), this attaches to a long-lived per-project tmux session — see
``orchestrator_shell``. Each WebSocket forkpty()'s into ``tmux attach-session``,
so the orchestrator survives reconnects and replays scrollback. When tmux is
unavailable the endpoint falls back to launching the harness argv directly.

SAFETY — a PTY is remote code execution scoped to whoever can reach the socket.
The server binds 127.0.0.1, but a browser tab on any origin can still open a
same-machine WebSocket (CORS does not gate ``ws://``). So the endpoint is
OPT-IN: it stays closed unless ``MO_PTY_ENABLED=1`` is set in the *serving*
process's environment. The harness is allow-listed by ``resolve_harness`` — a
query param can select a vetted program but never inject an arbitrary argv.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import signal
import struct
import termios
from pathlib import Path

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from ..deps import get_home_lenient
from ..orchestrator_shell import (
    build_attach_argv,
    ensure_session,
    harness_argv,
    resolve_harness,
    session_name,
    tmux_available,
)

router = APIRouter(prefix="/api/v1", tags=["pty"])

_FALSEY = ("", "0", "false", "no", "off")

# One asyncio lock per tmux session name so concurrent first-connects can't
# race two `new-session` spawns for the same project.
_session_locks: dict[str, asyncio.Lock] = {}


def pty_enabled() -> bool:
    return os.environ.get("MO_PTY_ENABLED", "").strip().lower() not in _FALSEY


def _resolve_cwd(run_id: str | None, home: Path) -> Path:
    """cwd for the shell: the run's own dir when it exists, else the project root.

    ``home`` is the project's ``.mini-ork`` dir, so ``home.parent`` is the
    project root — the natural cwd for a per-project orchestrator.
    """
    if run_id:
        run_dir = home / "runs" / run_id
        if run_dir.is_dir():
            return run_dir
    return home.parent


def _session_lock(name: str) -> asyncio.Lock:
    lock = _session_locks.get(name)
    if lock is None:
        lock = asyncio.Lock()
        _session_locks[name] = lock
    return lock


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


@router.websocket("/pty")
async def pty_socket(
    ws: WebSocket,
    run_id: str | None = Query(default=None),
    cmd: str = Query(default=""),
    home: str | None = Query(default=None),
    cols: int = Query(default=80, ge=1, le=1000),
    rows: int = Query(default=24, ge=1, le=1000),
) -> None:
    await ws.accept()
    if not pty_enabled():
        await ws.send_text(
            "\r\n\x1b[33m[pty disabled]\x1b[0m set MO_PTY_ENABLED=1 in the "
            "`mini-ork serve` process to enable shell access.\r\n"
        )
        await ws.close(code=4403)
        return

    project_home = get_home_lenient(None, home)
    harness = resolve_harness(cmd or None, project_home)
    cwd = _resolve_cwd(run_id, project_home)

    if tmux_available():
        # One long-lived session per (harness, project): spawn it once, then
        # this connection is just a thin attach client.
        name = session_name(harness, project_home)
        async with _session_lock(name):
            await asyncio.to_thread(ensure_session, name, harness, cwd, cols, rows)
        argv = build_attach_argv(name)
    else:
        # No tmux — run the harness directly; no reconnect persistence.
        argv = harness_argv(harness)

    pid, master_fd = os.forkpty()
    if pid == 0:  # child → become the shell
        try:
            os.chdir(cwd)
        except OSError:
            pass
        os.environ["TERM"] = "xterm-256color"
        try:
            os.execvp(argv[0], argv)
        except OSError as exc:  # command not found / not executable
            os.write(2, f"mini-ork pty: cannot exec {argv!r}: {exc}\r\n".encode())
        os._exit(127)

    # parent → bridge bytes between the PTY master and the WebSocket
    _set_winsize(master_fd, rows, cols)
    os.set_blocking(master_fd, False)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue()

    def _on_readable() -> None:
        try:
            data = os.read(master_fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""  # PTY closed (child exited)
        queue.put_nowait(data)

    loop.add_reader(master_fd, _on_readable)

    async def _pump_out() -> None:
        while True:
            data = await queue.get()
            if data == b"":  # EOF → child gone, tear the socket down
                await ws.close()
                return
            await ws.send_bytes(data)

    out_task = asyncio.create_task(_pump_out())
    try:
        while True:
            msg = await ws.receive_text()
            if not msg:
                continue
            op, payload = msg[0], msg[1:]
            if op == "0":  # keystroke / paste
                os.write(master_fd, payload.encode())
            elif op == "1":  # resize control frame
                try:
                    dims = json.loads(payload)
                    _set_winsize(master_fd, int(dims["rows"]), int(dims["cols"]))
                except (ValueError, KeyError, TypeError):
                    pass
    except WebSocketDisconnect:
        pass
    finally:
        loop.remove_reader(master_fd)
        out_task.cancel()
        for cleanup in (
            lambda: os.kill(pid, signal.SIGHUP),
            lambda: os.close(master_fd),
            lambda: os.waitpid(pid, os.WNOHANG),
        ):
            try:
                cleanup()
            except OSError:
                pass
