"""FastAPI app factory for the node-agent (kickoff requirement 1).

Composes the auth dependency, session/proc/engine/files routers, and
a reaper task into one ``FastAPI()`` instance. The CLI wrapper
(:mod:`mini_ork.cli.node_agent`) binds this to uvicorn after the
launcher-level bind / TLS / token-env checks succeed.

This factory is deliberately separate from
:func:`mini_ork.web.app.create_app`: the observability UI binds to
``127.0.0.1:7090`` and reads from ``.mini-ork/config/**``; the
node-agent binds to ``127.0.0.1:7091`` (default) and reads nothing
from that shadow. Sharing the app would couple two unrelated lifecycles
and surface ``MINI_ORK_HOME``-shadow regressions in the agent process.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from .auth import make_bearer_dependency
from .engines import EngineManager, manifest_for
from .files import UnsafeTarMember, _resolve_root, extract_tar, make_tar
from .procs import (
    ProcRegistry,
    ProcSpec,
    _RC_TIMEOUT,
)
from .sessions import SessionManager


def _state_dir_default() -> Path:
    raw = os.environ.get("MO_NODE_AGENT_STATE_DIR", "/srv/mini-ork")
    p = Path(raw)
    # If the requested state dir is on a read-only mount (CI / unit-test
    # sandboxes), fall back to a tmp path so the launcher doesn't crash
    # before it has a chance to print the bind summary. Production hosts
    # always have /srv/mini-ork writable; the env override lets the
    # launcher be testable without root.
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        p = Path("/tmp/mo-node-agent-state")
        p.mkdir(parents=True, exist_ok=True)
    return p


def create_app(
    *,
    state_dir: Path | None = None,
    token_env: str = "MO_NODE_TOKEN",
    runtime: str = "docker",
    retain_hours: float = 24.0,
    engine_root: Path | None = None,
    version: str = "0.1.0",
    _run_fn: Callable | None = None,
    _spawn_fn: Callable | None = None,
) -> FastAPI:
    """Build the node-agent FastAPI app.

    Args:
      state_dir: per-host root for ``runs/``, ``engines/``, ``sessions.json``.
      token_env: env var whose value is the bearer token. Read at request
        time so a rotation does not require a restart.
      runtime: ``docker`` (default) or ``host``.
      retain_hours: TTL for run dirs after a session is deleted.
      engine_root: source for the read-only engine mount at ``/opt/mini-ork``.
      version: server version reported by ``/v1/health``.
    """
    state_dir = Path(state_dir) if state_dir is not None else _state_dir_default()
    state_dir.mkdir(parents=True, exist_ok=True)

    bearer = make_bearer_dependency(token_env)

    sessions = SessionManager(
        state_dir, retain_hours=retain_hours, runtime=runtime,
        engine_root=engine_root, _run_fn=_run_fn,
    )
    engines = EngineManager(
        state_dir, version,
        session_count_fn=lambda: len(sessions._sessions),
        _run_fn=_run_fn,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        stop = asyncio.Event()

        async def _loop() -> None:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=60)
                    break
                except asyncio.TimeoutError:
                    pass
                try:
                    sessions.reap_expired()
                except Exception:  # reaper must never crash the server
                    pass
        task = asyncio.create_task(_loop())
        try:
            yield
        finally:
            stop.set()
            task.cancel()

    app = FastAPI(title="mini-ork node-agent", version=version, lifespan=lifespan)

    # ---- health (no auth) --------------------------------------------

    @app.get("/v1/health")
    def health() -> dict:
        docker_ok = shutil.which("docker") is not None
        return engines.health(docker_ok=docker_ok)

    # ---- sessions -----------------------------------------------------

    @app.post("/v1/sessions")
    def post_session(payload: dict, _: None = Depends(bearer)) -> dict:
        run_id = payload.get("run_id")
        image = payload.get("image")
        if not run_id or not image:
            raise HTTPException(400, "run_id and image required")
        try:
            sess = sessions.create(
                run_id=run_id, image=image,
                resources=payload.get("resources"),
                profile=payload.get("profile"),
            )
        except RuntimeError as exc:
            raise HTTPException(500, str(exc)) from exc
        return sess.to_dict()

    @app.delete("/v1/sessions/{sid}")
    def delete_session(sid: str, _: None = Depends(bearer)) -> dict:
        # Every other session route is addressed by run_id; accept it here too.
        ok = sessions.delete(sid)
        if not ok:
            by_run = sessions.by_run(sid)
            ok = by_run is not None and sessions.delete(by_run.sid)
        if not ok:
            raise HTTPException(404, f"unknown session {sid}")
        return {"deleted": sid}

    # ---- procs --------------------------------------------------------

    def _registry(run_id: str) -> ProcRegistry:
        sess = sessions.by_run(run_id)
        if sess is None or sess.proc_registry is None:
            raise HTTPException(404, f"no session for run_id {run_id}")
        return sess.proc_registry

    @app.post("/v1/sessions/{run_id}/procs")
    def post_proc(run_id: str, payload: dict, _: None = Depends(bearer)) -> dict:
        argv = payload.get("argv") or []
        if not isinstance(argv, list) or not argv:
            raise HTTPException(400, "argv must be a non-empty list")
        env = payload.get("env") or {}
        if not isinstance(env, dict):
            raise HTTPException(400, "env must be an object of string values")
        env = {str(k): str(v) for k, v in env.items()}
        env_keys = sorted(set(map(str, payload.get("env_keys") or [])) | set(env))
        spec = ProcSpec(
            argv=[str(a) for a in argv],
            env_keys=env_keys,
            env=env,
            cwd=payload.get("cwd"),
            stdin=str(payload.get("stdin", "")),
            timeout_s=payload.get("timeout_s"),
        )
        ps = _registry(run_id).spawn(spec)
        return {
            "pid": ps.pid,
            "state": ps.state,
            "rc": ps.rc,
        }

    @app.get("/v1/sessions/{run_id}/procs/{pid}")
    def get_proc(run_id: str, pid: int, _: None = Depends(bearer)) -> dict:
        ps = _registry(run_id).get(pid)
        if ps is None:
            raise HTTPException(404, f"unknown proc {pid}")
        return {
            "pid": ps.pid,
            "state": ps.state,
            "rc": ps.rc,
            "started_at": ps.started_at,
            "ended_at": ps.ended_at,
            "argv": ps.spec.argv,
            "env_keys": ps.spec.env_keys,
            "cwd": ps.spec.cwd,
            "out_offset": ps.out_offset,
            "err_offset": ps.err_offset,
        }

    @app.get("/v1/sessions/{run_id}/procs/{pid}/stream")
    def stream_proc(run_id: str, pid: int, request: Request,
                    _: None = Depends(bearer)) -> Response:
        """Byte-offset streaming endpoint.

        Returns chunked JSON lines ``{"stream","offset","data"}`` for
        bytes appended since the requested offset; the final line carries
        ``{"stream":"exit","rc":...,"state":"..."}``. ``Connection: close``
        after exit so a long-poll client doesn't hang.
        """
        ps = _registry(run_id).get(pid)
        if ps is None:
            raise HTTPException(404, f"unknown proc {pid}")
        out_off = int(request.query_params.get("out", "0"))
        err_off = int(request.query_params.get("err", "0"))

        def gen():
            reg = _registry(run_id)
            o, e = out_off, err_off
            while True:
                cur = reg.get(pid)
                if cur is None:
                    return
                out_chunk = reg.read_chunk(pid, stream="out", offset=o) or b""
                if out_chunk:
                    o += len(out_chunk)
                    yield json.dumps(
                        {"stream": "out", "offset": o, "data": out_chunk.decode("utf-8", "replace")}
                    ) + "\n"
                err_chunk = reg.read_chunk(pid, stream="err", offset=e) or b""
                if err_chunk:
                    e += len(err_chunk)
                    yield json.dumps(
                        {"stream": "err", "offset": e, "data": err_chunk.decode("utf-8", "replace")}
                    ) + "\n"
                if cur.state in ("exited", "killed", "timeout", "spawn_failed", "orphaned"):
                    yield json.dumps(
                        {"stream": "exit", "rc": cur.rc, "state": cur.state}
                    ) + "\n"
                    return
                time.sleep(0.05)

        return StreamingResponse(gen(), media_type="application/x-ndjson")

    @app.post("/v1/sessions/{run_id}/procs/{pid}/kill")
    def kill_proc(run_id: str, pid: int, _: None = Depends(bearer)) -> dict:
        ok = _registry(run_id).kill(pid)
        if not ok:
            raise HTTPException(404, f"unknown or exited proc {pid}")
        return {"killed": pid}

    # ---- exec (sync convenience) -------------------------------------

    @app.post("/v1/sessions/{run_id}/exec")
    def exec_sync(run_id: str, payload: dict, _: None = Depends(bearer)) -> dict:
        argv = payload.get("argv") or []
        if not isinstance(argv, list) or not argv:
            raise HTTPException(400, "argv must be a non-empty list")
        cwd = payload.get("cwd")
        timeout_s = float(payload.get("timeout_s", 60))
        reg = _registry(run_id)
        env = payload.get("env") or {}
        if not isinstance(env, dict):
            raise HTTPException(400, "env must be an object of string values")
        env = {str(k): str(v) for k, v in env.items()}
        spec = ProcSpec(argv=[str(a) for a in argv], env_keys=sorted(env), cwd=cwd, stdin="",
                        timeout_s=timeout_s, env=env)
        ps = reg.spawn(spec)
        # Wait synchronously for exit — the registry owns the lifecycle,
        # we just block the request thread until the proc reports done.
        deadline = time.time() + timeout_s + 5
        while True:
            cur = reg.get(ps.pid)
            if cur and cur.state in ("exited", "killed", "timeout", "spawn_failed", "orphaned"):
                break
            if time.time() > deadline:
                reg.kill(ps.pid)
                break
            time.sleep(0.05)
        cur = reg.get(ps.pid)
        out = reg.read_chunk(ps.pid, stream="out", offset=0) or b""
        err = reg.read_chunk(ps.pid, stream="err", offset=0) or b""
        return {
            "rc": cur.rc if cur else _RC_TIMEOUT,
            "output": (out + err).decode("utf-8", "replace"),
        }

    # ---- files --------------------------------------------------------

    @app.put("/v1/sessions/{run_id}/files")
    async def put_files(run_id: str, request: Request,
                        root: str, _: None = Depends(bearer)) -> dict:
        try:
            target = _resolve_root(state_dir, run_id, root)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        body = await request.body()
        try:
            count = extract_tar(target, body)
        except UnsafeTarMember as exc:
            raise HTTPException(400, f"unsafe tar member: {exc}") from exc
        return {"root": root, "extracted": count}

    @app.get("/v1/sessions/{run_id}/files")
    def get_files(run_id: str, root: str, _: None = Depends(bearer)) -> Response:
        try:
            target = _resolve_root(state_dir, run_id, root)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        body = make_tar(target)
        return Response(content=body, media_type="application/x-tar")

    @app.post("/v1/sessions/{run_id}/files/manifest")
    def files_manifest(run_id: str, root: str, _: None = Depends(bearer)) -> dict:
        try:
            target = _resolve_root(state_dir, run_id, root)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"root": root, "manifest": manifest_for(target)}

    # ---- tree (no-op-safe transport for epic 07) ----------------------

    @app.post("/v1/sessions/{run_id}/tree/bundle")
    async def put_tree_bundle(run_id: str, request: Request,
                              _: None = Depends(bearer)) -> dict:
        body = await request.body()
        target = state_dir / "runs" / run_id / "target" / ".mo-tree.bundle"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        return {"path": str(target), "bytes": len(body)}

    @app.get("/v1/sessions/{run_id}/tree/snapshot")
    def get_tree_snapshot(run_id: str, _: None = Depends(bearer)) -> Response:
        target = state_dir / "runs" / run_id / "target" / ".mo-tree.bundle"
        if not target.is_file():
            return Response(status_code=404)
        return Response(content=target.read_bytes(), media_type="application/x-git-bundle")

    # ---- engines ------------------------------------------------------

    @app.put("/v1/engines/{sha}")
    async def put_engine(sha: str, request: Request,
                         _: None = Depends(bearer)) -> dict:
        body = await request.body()
        try:
            state = engines.stage_bundle(sha, body)
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc
        return state.to_dict()

    return app


__all__ = ["create_app"]