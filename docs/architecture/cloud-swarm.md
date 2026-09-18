# Cloud-native swarm execution — spec

> Status: proposed (2026-09-18). Extends — does not replace —
> [`docs/epics/EPIC-cloud-exec-runtime-sandbox.md`](../epics/EPIC-cloud-exec-runtime-sandbox.md)
> (A1). That epic built the backend-swappable execution seam; this spec defines
> what it takes to run a whole mini-ork **swarm** (a run's nodes plus its
> recursive children) as a distributed fleet, and fixes the design posture so
> every future runtime change is cloud-compatible by construction.

## North star

A mini-ork run executes as a fleet: the control plane runs anywhere, each node
and each recursive child runs in a `Workspace` that may be on any host, and all
durable state lives on a `SharedDrive`. "Local" is the smallest backend, not the
architecture.

The test of the posture: **flipping a config knob — never editing code — moves
work from a laptop to a cloud fleet.** Today that is false in exactly six
places (gap register G1–G6 below).

## The posture — six invariants ("always think in the cloud")

These are review rules. Every change to the runtime, dispatch, or orchestration
layers is checked against them; a violation needs an explicit, documented
exception.

- **P1 — Placement-agnostic by default.** No new code path may assume same-host
  process spawn, host-local filesystem paths, or an inherited environment. A
  change that introduces one must name its cloud story (how it behaves when the
  workspace is remote) or be rejected.
- **P2 — Local is a backend, not the architecture.** The *default* backend
  stays `local` for migration safety (A1's delivery constraints: zero added
  overhead, degrade never fail, opt-in per run) — but that default is a rollout
  policy, not a design decision. Anything structurally impossible to run
  remotely is a defect even while the default is local.
- **P3 — The drive is the pet; agents are cattle.** Durable run state (target
  tree, run dir, artifacts, manifests) lives on the `SharedDrive`, never inside
  a sandbox. A sandbox can die at any moment; the run must be resumable from
  the drive (the invariant already stated in
  `mini_ork/runtime/shared_drive.py`).
- **P4 — Sync blocking is a smell.** Waiting on a child must be
  event-subscribable (`run_events` / SSE), not a process-tree join. A blocking
  wait couples placement (same host) and lifetime (parent waits) in one call —
  the exact coupling that makes distribution impossible.
- **P5 — Env is an allowlist, not an inheritance.** `{**os.environ, …}` in new
  code is a defect. Cross-boundary env rides the `container_env` allowlist
  (`mini_ork/runtime/backends/_workspace_env.py`: `MO_*` + provider prefixes +
  `*_API_KEY` suffix), and secrets come from a store, not inherited process
  env. `mini_ork/cli/spawn.py:388` is the known existing violation (G4).
- **P6 — Every backend passes the same conformance suite.** A new `Workspace`
  or `SharedDrive` backend ships with the conformance run green; the parity
  gate exercises every delegated entrypoint (lesson: a delegated-but-never-
  invoked seam is a silent no-op, not a migration).

## Plane architecture

Five planes. Each exists today in at least primitive form; the spec's job is to
name what each still lacks.

| Plane | Today | Cloud gap |
|---|---|---|
| **Control** | `mini-ork serve` (:7090): REST/SSE, detached launch (`control.launch_run`), steering (`control.steer_run`), agent-server shim | host-agnostic already; needs fleet-level run registry when drivers run apart from their children (S2) |
| **Execution** | `Workspace` protocol (`exec`/`spawn`/`put`/`get`/`up`/`down`, `mini_ork/runtime/sandbox.py:42`); backends `local`/`docker`/`microvm`; per-node selection (`agent_workspace.py`) | recursive children bypass the axis (G1); no remote backend (G5) |
| **Storage** | `SharedDrive` protocol; `local-bind` backend; run dirs + worktrees on host disk | drive is never network-backed (G3) |
| **Secrets** | `container_env` allowlist at the container boundary | recursive-spawn env inheritance leaks everything (G4); no secret-store injection |
| **Observability** | `run_events` → SSE; per-child `spawn-child.log` (`spawn.py:277`); durable-DAG checkpoints + leases; `sandbox-gc` reaper | supervision is local-process-shaped (G2); reaper is single-host (S5) |

## Gap register

| # | Seam | Violation | Fixed by |
|---|---|---|---|
| G1 | `mini_ork/cli/spawn.py:401` — recursive child launched via raw `subprocess.run` | bypasses the `Workspace` axis entirely; child is pinned to the parent's host (P1, P2) | S1 |
| G2 | `spawn()` blocks for the child's whole lifetime | parent/child lifetime + placement coupling; no fleet concurrency (P4) | S2 |
| G3 | child workspaces, run dirs (`<home>/runs/<id>`), worktrees on local disk | durable state unreachable from another host (P3) | S3 |
| G4 | `spawn.py:388` — `child_env = {**os.environ, …}` | full parent env (incl. secrets) crosses into every child (P5) | S1 |
| G5 | no remote `Workspace` backend registered | the knob that "moves work to the cloud" does not exist (P2) | S4 (creds-blocked) |
| G6 | no scheduler/caps for recursive children | a wave of N children fans out onto one host, ungoverned | S5 |

## Phases

Each phase is one scoped dispatch through the normal worktree loop, respects
A1's delivery-safety constraints (additive, default-off, degrade never fail),
and is independently verifiable. S1–S3 need **no cloud credentials** — they
make the fleet shape real locally, so the cloud is a backend swap rather than a
rewrite.

### S1 — Route recursive spawn through the Workspace axis (G1, G4)

The highest-leverage change: `spawn()`'s child launch stops being a special
case.

- When `MO_SANDBOX_BACKEND` is set, resolve the child's workspace the way
  dispatch already does (`resolve_spawn_workspace`, `agent_workspace.py`) and
  launch the child argv **inside it**: kickoff written via `workspace.put()`,
  child cwd = the in-container drive mount (`/workspace`), child env built by
  `container_env` (allowlist) instead of `{**os.environ}`. The recursive
  lineage keys (`MINI_ORK_RUN_ID`, `MINI_ORK_PARENT_RUN_ID`,
  `MINI_ORK_ALLOW_CHILD_SPAWN`) are `MO_*`-shaped and already cross the
  allowlist.
- The child image must carry mini-ork itself (the argv is `bin/mini-ork run
  …`). Ship a documented image expectation (mini-ork importable, provider CLIs
  present) rather than a mandatory custom image; `MO_SANDBOX_IMAGE` already
  selects it.
- Backend unset ⇒ byte-for-byte today's `subprocess.run` path (dead-code-proof
  default, matching `resolve_agent_workspace`'s discipline).
- `spawn-child.log` persists as today — streams come back from
  `Workspace.spawn` in the same `(rc, stdout, stderr)` shape.

**DoD:** a recursive spawn completes under `MO_SANDBOX_BACKEND=docker` with the
child's run dir appearing on the drive mount; env-leak test proves a
non-allowlisted parent env var is absent from the child; default path
byte-parity test green; unknown backend fails loud (never silent host
fallback).

### S2 — Detached children, event-supervised (G2)

Kill the blocking join. A child becomes something the parent *launches and
watches*, not something it *waits on*.

- `spawn()` delegates to the detached launch seam (`control.launch_run` — the
  same seam `POST /api/v1/runs` and the canvas conversation-create use) instead
  of `subprocess.run`. Locally this is already daemonize + log path; remotely
  it becomes an HTTP/UHP launch (S4) with **no change to the caller**.
- Supervision flips to events: parent subscribes to the child's `run_events`
  (SSE today, poll fallback) and detects completion from `task_runs` status +
  the durable-DAG lease — both already shipped. The `child.completed` /
  `child.failed` lineage events are emitted from observed state, not from
  process exit.
- Partial failure is covered by existing machinery: leases + idempotency +
  `--resume` at STEP/TURN granularity, and `sandbox-gc` for orphaned
  workspaces. A parent that dies does not lose children: the wave driver
  re-attaches by run id.

**DoD:** parent spawn returns before child completion; a child killed
mid-flight is detected via events and marked failed with its `spawn-child.log`
trail; the same test passes with the child on `local` and `docker` backends
(P6).

### S3 — Network shared drive (G3)

- Register a `SharedDrive` backend over network storage (NFS/JuiceFS, or a
  provider volume — `daytona-volume`/`e2b-volume` shapes were already
  anticipated in `shared_drive.py`). Run dirs, worktrees, and artifacts move
  onto the drive via the existing `MO_SHARED_DRIVE_ROOT` selector.
- Containment + "drive is pet" invariants are already enforced by the protocol
  (path containment, `down()` no-op default); the backend only adds transport.
- The artifact graph's run-local visibility limits carry over unchanged — the
  drive does not become a cross-run gossip channel.

**DoD:** a two-node run with each node in its own sandbox, communicating only
via the mounted drive, completes on two different hosts sharing the volume.

### S4 — Remote Workspace backend (G5; creds-blocked)

The actual cloud knob. Two candidate transports, decided at build time:

1. **Remote microsandbox server** — same SDK, different endpoint. The
   `microvm` backend's "same self-hosted server runs locally or in the cloud"
   property makes this the thinnest path.
2. **HTTP/UHP bridge** — a mini-ork agent-server inside the sandbox reached
   over HTTP (the A1 R3 "managed" pattern). This converges with Phase-B B3
   (mini-ork-as-UHP-server): a remote workspace is then just a UHP server
   hosting a mini-ork; S2's launch seam speaks it natively.

Either way the surface is unchanged: another `register_workspace_backend`
entry plus an endpoint config. **Blocked on cloud credentials** (per the
cloud-exec roadmap status); design shouldn't wait — S1–S3 are the
prerequisites and are unblocked.

### S5 — Fleet governance (G6)

- Wave scheduler: concurrency caps per backend, placement hints (cost/latency
  region per lane — the router's cost-aware bandit already models lane cost),
  and fleet-level RSI stops (GRAO/UCCI across a wave, not just a run).
- Per-child cost budgets: `task_runs` cost accounting exists; the spawn seam
  declares a budget and the scheduler refuses oversubscription instead of
  discovering it on the invoice.
- Cross-host `sandbox-gc`: reaper registry over the fleet, not one host.

## Conformance and testing

- **Workspace conformance suite** (one suite, every backend — P6): `exec`
  honors cwd + rc + merged streams; `spawn` keeps streams separate, feeds
  stdin, returns faithful rc (124 timeout / 127 spawn-fail); `put`/`get`
  round-trip; path containment rejects escapes; `up`/`down` idempotent;
  child env matches the allowlist exactly.
- **Recursive-spawn matrix**: spawn → child completes / child fails / child
  killed / parent killed, crossed with backend `local`/`docker` (remote added
  in S4). Every delegated entrypoint is actually invoked by the gate.
- **Default byte-parity**: backend unset ⇒ existing recursive-orchestration
  tests unchanged (the A1 "safe to sync" guarantee for vendored consumers).

## Non-goals / guards

- The default backend stays `local` until an explicit flip decision; nothing
  here force-migrates consumers (researcher's ~29-parallel-run cadence is the
  constraint A1 already documents).
- No multi-tenant auth in v1 of the remote backend — loopback/token posture
  first, same as the web control plane; hardening is its own epic.
- Recipes stay placement-free: isolation/placement is runtime-layer config,
  never per-recipe branching (`workflow.yaml` may *declare* requirements, e.g.
  `workspace: docker`, but never implement transport).
- No new hard dependencies on the default path — cloud SDKs import lazily at
  backend resolution, matching the existing lazy-import discipline.

## Open questions

1. S4 transport choice — remote microsandbox vs UHP bridge (decide when creds
   arrive; S2's launch seam is transport-agnostic either way).
2. Should the goal-loop wave driver gain a first-class "fleet" primitive
   (N drivers × M backends), or is driver-per-wave + scheduler caps enough?
   Decide after S2 lands and a real wave runs detached.
3. Artifact retention on network drives — lifecycle/GC policy for run dirs
   once they live on shared storage (disk is cheap locally, less so in the
   cloud).
