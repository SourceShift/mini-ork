# remote-nodes-06 — Remote Workspace backend + conformance suite

Part of `kickoffs/remote-nodes-roadmap.md`. Design: `docs/architecture/remote-nodes.md`
**D3** and **D8**, plus cloud-swarm **P6** (every backend passes one suite).
Depends on epic 02 (run-scoped session) and epic 05 (node-agent).

## Goal

A `Workspace` backend named `remote`, registered through
`register_workspace_backend` (`mini_ork/runtime/sandbox.py:164`), that drives a
node-agent over HTTP. Selecting it is a config change. One shared conformance
suite proves `local`, `docker` and `remote` behave the same at the protocol
boundary.

## Why

- The `Workspace` protocol (`mini_ork/runtime/sandbox.py:42`, verbs
  `exec`/`spawn`/`put`/`get`/`up`/`down`) is the seam the whole executor
  already routes through for isolated spawns (`dispatch/core.py:243`). A new
  backend is all that remains, with no change to callers.
- `resolve_spawn_workspace` (`runtime/agent_workspace.py:113-155`) only knows
  `local`/`docker`/`microvm`. Anything else falls to `get_workspace` and raises.
- cloud-swarm lists the conformance suite as a requirement (P6) but it does not
  exist yet. The S1 double-filter bug (`_RUN_CONTRACT_KEYS`) slipped through
  because unit tests used an in-process fake that did not behave like a real
  backend.

## Requirements

1. `mini_ork/runtime/backends/remote.py` implements `RemoteWorkspace` with
   stdlib `urllib` plus `http.client` streaming. No `requests`/`httpx`
   dependency on the default path. It imports lazily from the resolver.
   - `up()`: `GET /v1/health`, then compare `engine_shas` with the control
     plane's `git -C $MINI_ORK_ROOT rev-parse HEAD`. If the sha is missing,
     upload `git bundle create - HEAD` to `PUT /v1/engines/<sha>`. A dirty
     engine tree is refused unless `MO_REMOTE_ALLOW_DIRTY_ENGINE=1`, because a
     sha must name the exact code. Then `POST /v1/sessions`, which is
     idempotent per run id.
   - `spawn(argv, stdin, timeout, env, cwd)`: `POST …/procs`, then stream
     until exit, collecting stdout and stderr separately. Returns `(rc, out,
     err)` with rc semantics 124/127 preserved. Exposes an optional
     `on_chunk(stream, data)` hook that epic 09 wires; the default is a no-op.
   - `exec(cmd, cwd, timeout)` → `POST …/exec`. `put`/`get` → the files API.
   - `down()` → `DELETE /v1/sessions/{sid}`.
   - Transport errors are retried with jittered backoff (bounded by
     `MO_REMOTE_HTTP_RETRIES`, default 5). After exhaustion, raise
     `RemoteUnavailableError`, which dispatch maps to
     `failure_class=remote_unavailable`. Never fall back to the host.
2. Node registry, in a new module `mini_ork/remote/nodes.py`:
   - It reads `config/nodes.yaml` from `MINI_ORK_HOME/config`, falling back to
     `MINI_ORK_ROOT/config`. A shadow file **merges per node** with the
     template rather than replacing it, which avoids the known
     shadow-providers "drops lanes" trap.
   - Entry fields: `url`, `token_env`, `max_sessions`.
   - Env overrides: `MO_NODE=<name>`, or `MO_NODE_URL` + `MO_NODE_TOKEN` for a
     one-off.
   - Ship `config/nodes.yaml.example`. Tokens are only ever read from env.
3. Resolver: `resolve_spawn_workspace` and `resolve_agent_workspace` route
   `remote` to `RemoteWorkspace(node=..., run_id=..., image=...)`. `image`
   comes from `MO_SANDBOX_IMAGE`, overridable by the env profile (epic 12)
   once that lands.
4. Conformance suite `tests/unit/test_workspace_conformance.py`, parameterized
   over backends:
   - `local` always runs.
   - `docker` is daemon-gated.
   - `remote` runs against a **real in-process node-agent** (uvicorn on an
     ephemeral port in a thread, `--runtime host` from epic 05), so it needs no
     daemon and no fake.

   Cases, from cloud-swarm §Conformance:
   - `exec` honours cwd, rc and merged streams
   - `spawn` keeps streams separate, feeds stdin, and returns rc 0 / non-zero /
     124 on timeout / 127 on a bad argv[0]
   - `put`/`get` round-trip
   - `up`/`down` are idempotent
   - repeated `spawn` on one `up` works (epic 02)
   - the child env equals the allowlist plus the run contract exactly
5. Docs: append "Selecting the remote backend" to
   `docs/operator/remote-nodes.md`.

## Files in scope (touch ONLY these)

- `mini_ork/runtime/backends/remote.py` (new)
- `mini_ork/remote/nodes.py` (new)
- `mini_ork/runtime/agent_workspace.py` (resolver branch)
- `config/nodes.yaml.example` (new)
- `tests/unit/test_workspace_conformance.py` (new), `tests/unit/test_remote_workspace.py` (new)
- `docs/operator/remote-nodes.md` (append)

## Out of scope

- Moving the target tree or run dir (epics 07 and 08). In this epic, a remote
  spawn runs against whatever is already in the session dirs.
- Live tee to host files and cancellation propagation (epic 09).

## Verification command

```bash
python3.11 -m pytest -q tests/unit/test_workspace_conformance.py tests/unit/test_remote_workspace.py tests/unit/test_sandbox_protocol.py && make lint
```

## Acceptance

- The conformance suite is green for `local` and `remote` (in-process
  node-agent), and green for `docker` when a daemon is present.
- An engine sha mismatch triggers exactly one bundle upload. A second `up()`
  on the same sha uploads nothing.
- Node-agent down → `RemoteUnavailableError` after bounded retries, with no
  host execution. The test asserts no local subprocess ran.
- With `MO_SANDBOX_BACKEND` unset, `mini_ork.runtime.backends.remote` is never
  imported.

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
