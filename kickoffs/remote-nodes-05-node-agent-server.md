# remote-nodes-05 — Node-agent server (`mini-ork node-agent`)

Part of `kickoffs/remote-nodes-roadmap.md`. Design: `docs/architecture/remote-nodes.md`
decisions **D3**, **D7** and **D8**, plus the "Node-agent API (v1 sketch)"
section. Depends on epic 04 (image and engine layout).

## Goal

A small HTTP service you start on any Linux VM with Docker
(`mini-ork node-agent --bind <addr> --token-env MO_NODE_TOKEN`). It turns that
VM into a mini-ork data-plane host. It manages one session per run, runs
detached processes inside the session's container, buffers their output to disk
with byte offsets, and exposes everything over a token-authenticated API.

## Why

- No remote `Workspace` transport exists (cloud-swarm G5), and microsandbox
  remote mode is not wired: `set_default_backend` appears only in docstrings
  (`runtime/backends/microvm.py:14`, `:151`, `:178`).
- `DockerWorkspace` over `DOCKER_HOST=ssh://` breaks because `-v <path>`
  resolves on the remote host while `os.makedirs(drive_root)` runs locally
  (`docker.py:116`, `:124-125`). A service that runs *next to* Docker on the VM
  makes bind mounts correct by construction.
- Disconnect tolerance (D7) requires the process to outlive the HTTP request
  that started it. That needs a server-side process registry with durable
  output.

## Requirements

1. Package `mini_ork/remote/node_agent/` containing `app.py` (FastAPI app
   factory), `sessions.py`, `procs.py`, `engines.py` and `auth.py`. FastAPI
   and uvicorn are already used by `mini_ork/web/app.py`; add no new
   dependency.
2. **Sessions.**
   - `POST /v1/sessions {run_id, image, resources, profile?}` creates
     `/srv/mini-ork/runs/<run_id>/{target,run,home,mo-home}` (root
     configurable via `--state-dir`). It starts one container through the
     host-local `DockerWorkspace` with those dirs mounted at `/workspace/...`
     and the engine at `/opt/mini-ork:ro` (epic 04 layout).
   - Labels: `mo.sandbox=1`, `mo.run_id=<id>`.
   - Creating a session for an existing `run_id` returns the existing session
     (idempotent re-attach).
   - `DELETE /v1/sessions/{sid}` removes the container and keeps the run dirs
     until a TTL (`--retain-hours`, default 24).
   - A background reaper removes expired dirs and orphaned labelled containers.
3. **Procs.**
   - `POST …/procs {argv, env, cwd, stdin, timeout_s}` runs `docker exec -i`
     in the session container, detached from the request. stdout and stderr
     are appended to `<state>/runs/<id>/.procs/<pid>.{out,err}`. State and rc
     go to `<pid>.json`.
   - The in-container process runs under its own process group (`setsid`), so
     kill reaches the whole tree. The A.1 TTY-sever discipline from
     `dispatch/core.py:spawn_local` is re-established inside the container.
   - On timeout, the process group is killed, then rc=124 is recorded.
     Spawn failure records rc=127. These match the
     `core.dispatch` contract.
   - `GET …/procs/{pid}` returns state. `GET …/procs/{pid}/stream?out=&err=`
     streams new bytes from those offsets (chunked JSON lines
     `{stream, offset, data}`) until exit, then returns a final `{exit, rc}`
     record. `POST …/procs/{pid}/kill` kills the process group.
   - The proc registry survives a node-agent restart: it is rebuilt from
     `.procs/*.json`, and a still-running process is re-discovered by
     container plus pid.
   - Env values are never written to disk or logs: `<pid>.json` stores env
     **keys** only.
4. **Exec.** `POST …/exec {argv, cwd, timeout_s}` is a synchronous convenience
   built on procs. It returns `{rc, output}` (merged), matching
   `Workspace.exec`.
5. **Files and tree.**
   - `PUT/GET …/files?root=run|home|mo-home` transfers a tar body and
     extracts with path containment. Reject `..`, absolute paths and symlinks
     escaping the root.
   - `POST …/files/manifest?root=` returns `{relpath: sha256}`.
   - `POST …/tree/bundle` and `GET …/tree/snapshot` store and serve git
     bundles in the target dir. Epic 07 defines the git semantics; this epic
     ships the transport plus a no-op-safe implementation: `git bundle
     verify`, then fetch into `refs/mo/sync/*`.
6. **Engines.** `PUT /v1/engines/{sha}` takes a git bundle of the control
   plane's `MINI_ORK_ROOT`. The server verifies it, runs `git archive` at
   `<sha>` into `engines/<sha>`, and runs `python3 -c "import mini_ork"`
   against it. `GET /v1/health` returns `{version, engine_shas,
   docker_ok, sessions, capacity}`.
7. **Auth and bind.**
   - Every route except `/v1/health` requires `Authorization: Bearer
     <token>`. The token is read from the env var named by `--token-env`.
     Compare with constant-time `hmac.compare_digest`.
   - Default bind is `127.0.0.1:7091`.
   - A non-loopback, non-tailnet (`100.64.0.0/10`) bind requires
     `--tls-cert/--tls-key`, or it refuses to start.
8. **Runtimes.** `--runtime docker` is the default and described above.
   `--runtime host` runs procs directly on the VM under the session dir, with
   the same API, path roots via symlinks or cwd, and process-group discipline.
   It exists so the conformance suite (epic 06) and CI can drive a real
   node-agent without a Docker daemon. It also serves trusted single-tenant
   VMs. Log a startup WARN that `host` gives no isolation.
9. **Subcommand registration.** Add `"node-agent":
   "mini_ork.cli.node_agent"` to `_NATIVE_MODULE_SUBS`
   (`mini_ork/cli/main.py:40`) and update the exact-set guard in
   `tests/unit/test_native_dispatch_py.py` in the same change.

## Files in scope (touch ONLY these)

- `mini_ork/remote/__init__.py`, `mini_ork/remote/node_agent/*` (new)
- `mini_ork/cli/node_agent.py` (new)
- `mini_ork/cli/main.py` (`_NATIVE_MODULE_SUBS` entry only)
- `tests/unit/test_native_dispatch_py.py` (exact-set guard)
- `tests/unit/test_node_agent.py` (new)
- `docs/operator/remote-nodes.md` (append "Running a node-agent")

## Out of scope

- The client side (epic 06), git sync semantics (epic 07), setup-script
  caching and network policy (epic 12).

## Verification command

```bash
python3.11 -m pytest -q tests/unit/test_node_agent.py tests/unit/test_native_dispatch_py.py && make lint
```

## Acceptance

Use FastAPI `TestClient` with a fake Docker runner injected through one `_run(argv)`
seam, the same pattern as `sandbox_reaper.py`, so the unit tests need no
daemon.
- Missing or wrong token → 401 on every non-health route.
- Detached-proc lifecycle:
  - start a proc and read the stream from offset 0
  - drop the client and reconnect with the last offset; no bytes are
    duplicated or lost
  - read the final rc
- Timeout → rc=124 and the process group is killed. Kill → state `killed`.
- After the server object restarts, the proc registry is rebuilt from disk.
- Tar extraction rejects `../escape` and absolute members.
- A bind of `0.0.0.0` without TLS refuses to start.
- Daemon-gated live test: a real container session runs `sh -c 'echo hi;
  sleep 1; echo bye'` and the stream yields both lines.

## Review bar (added after epics 02–04 were reviewed)

Each earlier epic passed its gate and still failed review for the same reason:
the acceptance tests that drive the REAL seam were never written, so unwired or
broken code looked green. This epic is rejected unless:

- every Acceptance bullet above has its own test, and that test exercises the
  production path (the executor / dispatch / CLI entrypoint), not only the new
  module in isolation;
- every new public function or class has at least one production caller
  (`git grep` for it outside its own module and tests);
- nothing is gated only by a skip: a daemon- or network-gated test must run in
  this environment (Docker is available via colima; only `/Volumes/docker-ssd`
  is bind-visible), or the epic says why it cannot;
- the default path (feature env unset) is proven byte-identical by a test.
