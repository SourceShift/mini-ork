# R3a: docker runtime backend (opt-in, degrade-never-fail)

## Context
Epic `docs/epics/EPIC-cloud-exec-runtime-sandbox.md`, phase R3 (first slice: docker). The runtime
seam exists (`lib/runtime/contract.sh` → `mo_runtime_exec/put/get/start/stop/alive`, factory on
`MO_RUNTIME_BACKEND`, default `local`) with `local` (R0a) and `bubblewrap` (R2) backends. This
phase adds a `docker` backend: run each node's command in a per-run container, with real file
transfer. This is the stepping stone to managed cloud (R3b, separate).

Reference: `internal-docs/research/impl-analysis/02-managed-sandbox-e2b-openhands.md` and
`01-runtime-sandbox-swerex-minisweagent.md` (mini-swe-agent's DockerEnvironment: start container
once, `docker exec -w <cwd>` per call).

## Hard constraints (delivery-safety — see epic)
- **Opt-in only.** Default stays `local`. This backend runs ONLY when `MO_RUNTIME_BACKEND=docker`.
- **Degrade, never fail.** If `docker` is not on PATH or the daemon is unreachable, fall back to
  the `local` backend with a one-line WARN — never abort a run. (Mirror `lib/runtime/bubblewrap.sh`'s
  availability check + fall-back pattern.)
- Per-run container; no new global lock; concurrency-safe (~29 parallel runs).

## Deliverables
1. `lib/runtime/docker.sh` implementing the contract:
   - `mo_runtime_start`: `docker run -d` a long-lived container (image from `MO_RUNTIME_DOCKER_IMAGE`,
     default a small ubuntu/debian with bash) with the workspace bind-mounted at the same path;
     record the container id in the run dir.
   - `mo_runtime_exec "<cmd>" "<cwd>" [timeout]`: `docker exec -w "<cwd>" <cid> bash -c "<cmd>"`;
     preserve exit code + stdout; enforce timeout (kill the exec). Reuse the contract's result shape.
   - `mo_runtime_put/get`: `docker cp` in/out.
   - `mo_runtime_stop`: `docker rm -f` the container (best-effort).
   - `docker_available()` helper (`command -v docker` + `docker info` reachable) → fall back to
     `local` with WARN when false.
2. Register `docker` in the factory in `lib/runtime/contract.sh` (minimal).

## Smoke / DoD (must pass)
- `bash -n lib/runtime/docker.sh lib/runtime/contract.sh` clean.
- `tests/unit/test_runtime_docker.sh`:
  - If docker available: `mo_runtime_exec` runs a command in the container and returns correct
    rc/stdout; `put` then `get` round-trips a file; a write inside the bind-mounted workspace is
    visible on the host.
  - If docker NOT available: assert `MO_RUNTIME_BACKEND=docker mo_runtime_exec` still runs the
    command (fell back to local) and emits the WARN. Use `_skip` for the container assertions when
    docker is absent, but the fall-back assertion must run.
- Existing `test_runtime_contract.sh` (6/6), `test_runtime_bubblewrap.sh`, `test_executor_runtime_routing.sh`
  still green; `pytest` still green. Default behavior unchanged.

## Constraints (scope guard)
- Touch ONLY `lib/runtime/docker.sh`, the factory line in `lib/runtime/contract.sh`, and the new test.
- Do NOT change the default backend, `bin/mini-ork-execute`, recipes, the minimal agent, or other backends.
- No managed-cloud / agent-server-in-sandbox here — that is R3b.
